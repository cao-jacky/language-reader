"""Backend settings, editable from the Settings page and stored in SQLite.

Precedence for each key: value saved in the database, then an environment
variable, then the built-in default. API keys are never sent back to the
browser, only whether one is set and its last four characters.
"""
from __future__ import annotations

import os

from . import db

PROVIDERS = {"anthropic", "openai", "none"}
PASSAGE_PROVIDERS = {"llm", "deepl"}
EFFORTS = {"", "low", "medium", "high", "xhigh", "max"}

DEFAULTS = {
    "llm_provider": "anthropic",
    "llm_model": "claude-haiku-5-5",
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_effort": "low",
    "llm_timeout": "60",
    "passage_provider": "llm",
    "deepl_api_key": "",
    "target_language": "English",
}

ENV = {
    "llm_provider": ("LLM_PROVIDER",),
    "llm_model": ("LLM_MODEL",),
    "llm_base_url": ("LLM_BASE_URL",),
    "llm_api_key": ("LLM_API_KEY", "ANTHROPIC_API_KEY"),
    "llm_effort": ("LLM_EFFORT",),
    "passage_provider": ("PASSAGE_PROVIDER",),
    "deepl_api_key": ("DEEPL_API_KEY",),
    "target_language": ("TARGET_LANGUAGE",),
}

SECRETS = ("llm_api_key", "deepl_api_key")


def _env(key: str) -> str | None:
    for name in ENV.get(key, ()):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return None


def load() -> dict[str, str]:
    with db.tx() as c:
        saved = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}
    out = {}
    for k, default in DEFAULTS.items():
        v = saved.get(k)
        out[k] = v if v not in (None, "") else (_env(k) or default)
    return out


def public() -> dict:
    s = load()
    with db.tx() as c:
        saved = {r["key"] for r in c.execute("SELECT key FROM settings WHERE value <> ''")}
    out: dict = {k: v for k, v in s.items() if k not in SECRETS}
    for k in SECRETS:
        v = s[k]
        out[k] = {
            "set": bool(v),
            "hint": f"…{v[-4:]}" if len(v) >= 8 else ("set" if v else ""),
            "source": "saved" if k in saved else ("environment" if v else None),
        }
    return out


def save(values: dict, clear: list[str] | None = None) -> None:
    errors = []
    clean: dict[str, str] = {}
    for k, v in values.items():
        if k not in DEFAULTS or v is None:
            continue
        v = str(v).strip()
        if k in SECRETS and not v:
            continue  # empty secret field means "keep what is saved"
        clean[k] = v
    if "llm_provider" in clean and clean["llm_provider"] not in PROVIDERS:
        errors.append("llm_provider must be one of " + ", ".join(sorted(PROVIDERS)))
    if "passage_provider" in clean and clean["passage_provider"] not in PASSAGE_PROVIDERS:
        errors.append("passage_provider must be llm or deepl")
    if "llm_effort" in clean and clean["llm_effort"] not in EFFORTS:
        errors.append("llm_effort must be blank or one of low, medium, high, xhigh, max")
    if "llm_timeout" in clean:
        try:
            if not 5 <= int(clean["llm_timeout"]) <= 600:
                raise ValueError
        except ValueError:
            errors.append("llm_timeout must be a whole number of seconds between 5 and 600")
    if "llm_base_url" in clean and clean["llm_base_url"] and not clean["llm_base_url"].startswith(("http://", "https://")):
        errors.append("llm_base_url must start with http:// or https://")
    if errors:
        raise ValueError("; ".join(errors))
    with db.tx() as c:
        for k, v in clean.items():
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, v),
            )
        for k in clear or []:
            if k in SECRETS:
                c.execute("DELETE FROM settings WHERE key = ?", (k,))
