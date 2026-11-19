"""Server-side text-to-speech with Piper (https://github.com/OHF-Voice/piper1-gpl).

Piper is a small neural TTS engine that runs offline on the CPU. Voices are
downloaded on request from the official catalogue on Hugging Face into
$DATA_DIR/piper-voices (about 60 MB each) and generated audio is cached in
$DATA_DIR/tts-cache, so a sentence is only ever synthesised once per voice
and speed.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import threading
import time
import urllib.request
import wave
from collections import OrderedDict
from pathlib import Path

from . import db

CATALOGUE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
FILE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/{path}"
VOICES_DIR = db.DATA_DIR / "piper-voices"
CACHE_DIR = db.DATA_DIR / "tts-cache"
CATALOGUE_FILE = VOICES_DIR / "voices.json"
CATALOGUE_TTL = 7 * 24 * 3600
MAX_TEXT = 2000
KEY_RE = re.compile(r"^[a-z]{2,3}_[A-Z]{2}-[A-Za-z0-9_]+-(x_low|low|medium|high)$")


class TTSError(Exception):
    pass


def available() -> bool:
    try:
        import piper  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------- #
# catalogue
# --------------------------------------------------------------------------- #

_cat_lock = threading.Lock()


def catalogue() -> dict:
    """voices.json from Hugging Face, cached on disk for a week."""
    with _cat_lock:
        fresh = CATALOGUE_FILE.exists() and time.time() - CATALOGUE_FILE.stat().st_mtime < CATALOGUE_TTL
        if not fresh:
            try:
                VOICES_DIR.mkdir(parents=True, exist_ok=True)
                with urllib.request.urlopen(CATALOGUE_URL, timeout=20) as r:
                    data = r.read()
                json.loads(data)  # validate before replacing the cached copy
                tmp = CATALOGUE_FILE.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(CATALOGUE_FILE)
            except Exception as e:  # noqa: BLE001
                if not CATALOGUE_FILE.exists():
                    raise TTSError(f"Couldn't fetch the Piper voice list: {e}") from e
        return json.loads(CATALOGUE_FILE.read_text(encoding="utf-8"))


def _describe(key: str, entry: dict | None) -> dict:
    files = (entry or {}).get("files", {})
    size = sum(f["size_bytes"] for p, f in files.items() if p.endswith(".onnx"))
    return {
        "key": key,
        "name": (entry or {}).get("name", key.split("-")[1] if "-" in key else key).replace("_", " ").title(),
        "quality": (entry or {}).get("quality", key.rsplit("-", 1)[-1]),
        "region": (entry or {}).get("language", {}).get("country_english", ""),
        "size_mb": round(size / 1_000_000) if size else None,
        "installed": (VOICES_DIR / f"{key}.onnx").exists(),
    }


def voices_for(lang: str) -> list[dict]:
    """Every catalogue voice for a language family (ru, fi…), installed ones first."""
    try:
        cat = catalogue()
    except TTSError:
        cat = {}
    keys = {k for k, e in cat.items() if e.get("language", {}).get("family") == lang}
    keys |= {k for k in installed() if k.split("_", 1)[0] == lang}
    quality_rank = {"high": 0, "medium": 1, "low": 2, "x_low": 3}
    out = [_describe(k, cat.get(k)) for k in keys]
    return sorted(out, key=lambda v: (not v["installed"], quality_rank.get(v["quality"], 9), v["name"]))


def installed() -> list[str]:
    if not VOICES_DIR.exists():
        return []
    return sorted(p.name[: -len(".onnx")] for p in VOICES_DIR.glob("*.onnx") if (p.parent / f"{p.name}.json").exists())


# --------------------------------------------------------------------------- #
# downloads (background threads; progress polled by the settings page)
# --------------------------------------------------------------------------- #

_downloads: dict[str, dict] = {}
_dl_lock = threading.Lock()


def download_status() -> dict:
    with _dl_lock:
        return {k: dict(v) for k, v in _downloads.items()}


def start_download(key: str) -> None:
    if not KEY_RE.match(key):
        raise TTSError("Not a Piper voice name")
    entry = catalogue().get(key)
    if not entry:
        raise TTSError(f"{key} isn't in the Piper voice list")
    with _dl_lock:
        if _downloads.get(key, {}).get("state") == "downloading":
            return
        _downloads[key] = {"state": "downloading", "done": 0, "total": 0, "error": None}
    threading.Thread(target=_download, args=(key, entry), daemon=True).start()


def _download(key: str, entry: dict) -> None:
    files = [p for p in entry["files"] if p.endswith((".onnx", ".onnx.json"))]
    total = sum(entry["files"][p]["size_bytes"] for p in files)
    with _dl_lock:
        _downloads[key]["total"] = total
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        done = 0
        # The .json goes last: a voice counts as installed only when both exist.
        for path in sorted(files, key=lambda p: p.endswith(".json")):
            dest = VOICES_DIR / Path(path).name
            tmp = dest.with_name(dest.name + ".part")
            digest = hashlib.md5()  # the catalogue publishes md5 digests
            with urllib.request.urlopen(FILE_URL.format(path=path), timeout=60) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 16):
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    with _dl_lock:
                        _downloads[key]["done"] = done
            want = entry["files"][path].get("md5_digest")
            if want and digest.hexdigest() != want:
                tmp.unlink(missing_ok=True)
                raise TTSError(f"Checksum mismatch for {dest.name}")
            tmp.replace(dest)
        with _dl_lock:
            _downloads[key]["state"] = "done"
    except Exception as e:  # noqa: BLE001
        for path in files:
            (VOICES_DIR / (Path(path).name + ".part")).unlink(missing_ok=True)
        with _dl_lock:
            _downloads[key].update(state="error", error=str(e))


def remove(key: str) -> None:
    if not KEY_RE.match(key):
        raise TTSError("Not a Piper voice name")
    for suffix in (".onnx", ".onnx.json"):
        (VOICES_DIR / f"{key}{suffix}").unlink(missing_ok=True)
    with _voice_lock:
        _loaded.pop(key, None)
    shutil.rmtree(CACHE_DIR / key, ignore_errors=True)
    with _dl_lock:
        _downloads.pop(key, None)


# --------------------------------------------------------------------------- #
# synthesis
# --------------------------------------------------------------------------- #

_loaded: OrderedDict = OrderedDict()  # key -> PiperVoice, most recently used last
_voice_lock = threading.Lock()
_synth_lock = threading.Lock()  # onnxruntime already uses all cores; one job at a time


def _voice(key: str):
    with _voice_lock:
        if key in _loaded:
            _loaded.move_to_end(key)
            return _loaded[key]
    model = VOICES_DIR / f"{key}.onnx"
    if not model.exists():
        raise TTSError(f"The voice {key} isn't downloaded")
    from piper import PiperVoice

    v = PiperVoice.load(model)
    with _voice_lock:
        _loaded[key] = v
        while len(_loaded) > 3:  # each loaded voice holds ~100 MB
            _loaded.popitem(last=False)
    return v


def synthesize(text: str, key: str, speed: float = 1.0) -> bytes:
    """WAV audio for text, from the cache when possible."""
    text = " ".join(text.split())[:MAX_TEXT]
    if not text:
        raise TTSError("Nothing to read")
    if not KEY_RE.match(key):
        raise TTSError("Not a Piper voice name")
    speed = min(max(speed, 0.5), 2.0)
    name = hashlib.sha256(f"{speed:.2f}\n{text}".encode()).hexdigest()[:32]
    path = CACHE_DIR / key / f"{name}.wav"
    if path.exists():
        return path.read_bytes()
    from piper import SynthesisConfig

    voice = _voice(key)
    buf = io.BytesIO()
    with _synth_lock, wave.open(buf, "wb") as w:
        voice.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=1 / speed))
    data = buf.getvalue()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return data


def cache_size() -> int:
    if not CACHE_DIR.exists():
        return 0
    return sum(p.stat().st_size for p in CACHE_DIR.rglob("*.wav"))


def clear_cache() -> None:
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
