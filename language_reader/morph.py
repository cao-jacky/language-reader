"""Optional local morphological analysers.

pymorphy3 (Russian) and Voikko (Finnish) run offline. When present, their
candidate dictionary forms are passed to the LLM as hints, which noticeably
improves lemma accuracy for small models. Both are optional: without them the
LLM does the analysis on its own.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_loaded = False
_ru = None
_fi = None
_errors: dict[str, str] = {}

_FI_KEYS = ("CLASS", "SIJAMUOTO", "NUMBER", "MOOD", "TENSE", "PERSON", "COMPARISON", "WORDBASES")


def _load() -> None:
    global _loaded, _ru, _fi
    with _lock:
        if _loaded:
            return
        try:
            import pymorphy3

            _ru = pymorphy3.MorphAnalyzer(lang="ru")
        except Exception as e:  # noqa: BLE001 - any failure just disables it
            _errors["ru"] = f"{type(e).__name__}: {e}"
        try:
            from libvoikko import Voikko

            _fi = Voikko("fi")
        except Exception as e:  # noqa: BLE001
            _errors["fi"] = f"{type(e).__name__}: {e}"
        _loaded = True


def candidates(lang: str, word: str, limit: int = 4) -> list[str]:
    _load()
    out: list[str] = []
    try:
        if lang == "ru" and _ru is not None:
            for p in _ru.parse(word)[:limit]:
                out.append(f"{p.normal_form} ({p.tag})")
        elif lang == "fi" and _fi is not None:
            for a in _fi.analyze(word)[:limit]:
                feats = ", ".join(f"{k}={a[k]}" for k in _FI_KEYS if k in a)
                out.append(f"{a.get('BASEFORM', '?')} ({feats})")
    except Exception:  # noqa: BLE001 - hints are best effort
        return []
    return list(dict.fromkeys(out))


def status() -> dict:
    _load()
    return {
        "ru": {"name": "pymorphy3", "available": _ru is not None, "error": _errors.get("ru")},
        "fi": {"name": "Voikko", "available": _fi is not None, "error": _errors.get("fi")},
    }
