"""Pluggable backends.

- anthropic: the Claude API through the official SDK, with JSON-schema
  structured outputs so replies always parse.
- openai: any OpenAI-compatible /chat/completions endpoint (Ollama, LM Studio,
  vLLM, llama.cpp server, OpenRouter, OpenAI…). The schema is described in the
  prompt and the reply is parsed leniently.
- deepl: sentence translation only, used for whole passages when selected.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import usage


class BackendError(Exception):
    pass


# --------------------------------------------------------------------------- #
# structured completion
# --------------------------------------------------------------------------- #


def complete_json(s: dict, system: str, user: str, schema: dict, max_tokens: int = 2000, task: str = "other") -> dict:
    provider = s["llm_provider"]
    if provider == "none":
        raise BackendError("No LLM backend is configured. Choose one on the Settings page.")
    if not s["llm_model"]:
        raise BackendError("No model is set. Enter one on the Settings page.")
    if provider == "anthropic":
        data, tokens = _anthropic(s, system, user, schema, max_tokens)
    elif provider == "openai":
        data, tokens = _openai(s, system, user, schema, max_tokens)
    else:
        raise BackendError(f"Unknown provider {provider!r}")
    usage.record(s, provider=provider, model=s["llm_model"], task=task,
                 input_tokens=tokens[0], output_tokens=tokens[1])
    return conform(data, schema)


def _anthropic(s: dict, system: str, user: str, schema: dict, max_tokens: int) -> dict:
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise BackendError("The anthropic package is not installed") from e

    client = anthropic.Anthropic(
        api_key=s["llm_api_key"] or None,  # None lets the SDK read ANTHROPIC_API_KEY
        base_url=s["llm_base_url"] or None,
        timeout=float(s["llm_timeout"]),
        max_retries=2,
    )
    output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
    if s["llm_effort"]:
        output_config["effort"] = s["llm_effort"]
    try:
        resp = client.messages.create(
            model=s["llm_model"],
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
    except anthropic.AuthenticationError as e:
        raise BackendError("Claude API rejected the API key (401). Check it on the Settings page.") from e
    except anthropic.NotFoundError as e:
        raise BackendError(f"Claude API: model {s['llm_model']!r} not found (404).") from e
    except anthropic.RateLimitError as e:
        raise BackendError("Claude API rate limit reached (429). Try again shortly.") from e
    except anthropic.APIStatusError as e:
        raise BackendError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise BackendError(f"Could not reach the Claude API: {e}") from e

    if resp.stop_reason == "refusal":
        raise BackendError("The model declined this request.")
    if resp.stop_reason == "max_tokens":
        raise BackendError("The reply was cut off (max_tokens). Try a shorter selection.")
    u = resp.usage
    tokens = (
        (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0),
        u.output_tokens or 0,  # includes thinking, which is billed as output
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return parse_json(text), tokens


def _openai(s: dict, system: str, user: str, schema: dict, max_tokens: int) -> dict:
    base = (s["llm_base_url"] or "https://api.openai.com/v1").rstrip("/")
    url = base + "/chat/completions"
    user = (
        f"{user}\n\nReply with one JSON object only, no prose and no code fences, "
        f"matching this JSON schema:\n{json.dumps(schema, ensure_ascii=False)}"
    )
    body = {
        "model": s["llm_model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if s["llm_api_key"]:
        headers["Authorization"] = f"Bearer {s['llm_api_key']}"
    timeout = float(s["llm_timeout"])
    try:
        data = _post_json(url, body, headers, timeout)
    except _HTTPError as e:
        if e.status != 400:
            raise BackendError(f"{url} returned {e.status}: {e.detail}") from e
        # Some servers reject response_format or max_tokens; retry with the bare minimum.
        minimal = {"model": body["model"], "messages": body["messages"]}
        try:
            data = _post_json(url, minimal, headers, timeout)
        except _HTTPError as e2:
            raise BackendError(f"{url} returned {e2.status}: {e2.detail}") from e2
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise BackendError(f"Unexpected reply shape from {url}") from e
    u = data.get("usage") or {}
    tokens = (u.get("prompt_tokens"), u.get("completion_tokens"))  # None when the server doesn't report them
    return parse_json(text), tokens


def chat(s: dict, system: str, messages: list[dict], max_tokens: int = 2000, task: str = "chat") -> tuple[str, tuple]:
    """A free-text, multi-turn reply. Returns (text, (input_tokens, output_tokens))."""
    provider = s["llm_provider"]
    if provider == "none":
        raise BackendError("No LLM backend is configured. Choose one on the Settings page.")
    if provider == "anthropic":
        text, tokens = _anthropic_chat(s, system, messages, max_tokens)
    elif provider == "openai":
        text, tokens = _openai_chat(s, system, messages, max_tokens)
    else:
        raise BackendError(f"Unknown provider {provider!r}")
    usage.record(s, provider=provider, model=s["llm_model"], task=task, input_tokens=tokens[0], output_tokens=tokens[1])
    return text, tokens


def _anthropic_chat(s: dict, system: str, messages: list[dict], max_tokens: int) -> tuple[str, tuple]:
    import anthropic

    client = anthropic.Anthropic(
        api_key=s["llm_api_key"] or None, base_url=s["llm_base_url"] or None,
        timeout=float(s["llm_timeout"]), max_retries=2,
    )
    kwargs: dict = {}
    if s["llm_effort"]:
        kwargs["output_config"] = {"effort": s["llm_effort"]}
    try:
        resp = client.messages.create(
            model=s["llm_model"], max_tokens=max_tokens, system=system, messages=messages,
            # Caches the conversation so far; each new turn re-reads it at a tenth of the price.
            cache_control={"type": "ephemeral"},
            **kwargs,
        )
    except anthropic.AuthenticationError as e:
        raise BackendError("Claude API rejected the API key (401). Check it on the Settings page.") from e
    except anthropic.NotFoundError as e:
        raise BackendError(f"Claude API: model {s['llm_model']!r} not found (404).") from e
    except anthropic.RateLimitError as e:
        raise BackendError("Claude API rate limit reached (429). Try again shortly.") from e
    except anthropic.APIStatusError as e:
        raise BackendError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise BackendError(f"Could not reach the Claude API: {e}") from e
    if resp.stop_reason == "refusal":
        raise BackendError("The model declined this request.")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if resp.stop_reason == "max_tokens":
        text += "\n\n*(The answer was cut off.)*"
    u = resp.usage
    tokens = (
        (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0),
        u.output_tokens or 0,
    )
    return text, tokens


def _openai_chat(s: dict, system: str, messages: list[dict], max_tokens: int) -> tuple[str, tuple]:
    base = (s["llm_base_url"] or "https://api.openai.com/v1").rstrip("/")
    url = base + "/chat/completions"
    body = {"model": s["llm_model"], "messages": [{"role": "system", "content": system}, *messages], "max_tokens": max_tokens}
    headers = {"Content-Type": "application/json"}
    if s["llm_api_key"]:
        headers["Authorization"] = f"Bearer {s['llm_api_key']}"
    timeout = float(s["llm_timeout"])
    try:
        data = _post_json(url, body, headers, timeout)
    except _HTTPError as e:
        if e.status != 400:
            raise BackendError(f"{url} returned {e.status}: {e.detail}") from e
        try:
            data = _post_json(url, {"model": body["model"], "messages": body["messages"]}, headers, timeout)
        except _HTTPError as e2:
            raise BackendError(f"{url} returned {e2.status}: {e2.detail}") from e2
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise BackendError(f"Unexpected reply shape from {url}") from e
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    u = data.get("usage") or {}
    return text, (u.get("prompt_tokens"), u.get("completion_tokens"))


def parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # reasoning models
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(text[a : b + 1])
        except json.JSONDecodeError:
            pass
    raise BackendError("The model did not return valid JSON. A larger model may help.")


def conform(data, schema: dict):
    """Fill missing or wrongly typed fields so callers can rely on the shape."""
    if not isinstance(data, dict):
        data = {}
    out = {}
    for k, spec in schema.get("properties", {}).items():
        v = data.get(k)
        t = spec.get("type")
        if t == "array":
            if not isinstance(v, list):
                v = [] if v in (None, "") else [v]
            v = [str(x) for x in v if x not in (None, "")]
        elif t == "string":
            v = "" if v is None else (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
            if "enum" in spec and v not in spec["enum"]:
                v = v.strip().upper() if v.strip().upper() in spec["enum"] else ""
        out[k] = v
    return out


# --------------------------------------------------------------------------- #
# DeepL
# --------------------------------------------------------------------------- #

DEEPL_TARGETS = {
    "english": "EN-GB", "british english": "EN-GB", "american english": "EN-US",
    "finnish": "FI", "swedish": "SV", "german": "DE", "french": "FR", "spanish": "ES",
    "italian": "IT", "dutch": "NL", "polish": "PL", "estonian": "ET", "russian": "RU",
    "ukrainian": "UK", "chinese": "ZH-HANS", "japanese": "JA", "korean": "KO",
    "portuguese": "PT-PT", "danish": "DA", "norwegian": "NB",
}


def deepl_translate(s: dict, texts: list[str], source_lang: str) -> list[str]:
    key = s["deepl_api_key"]
    if not key:
        raise BackendError("DeepL is selected for passages but no DeepL API key is set.")
    target = DEEPL_TARGETS.get(s["target_language"].strip().lower())
    if not target:
        raise BackendError(f"DeepL does not know the target language {s['target_language']!r}.")
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    out: list[str] = []
    for i in range(0, len(texts), 50):  # DeepL accepts up to 50 texts per request
        chunk = texts[i : i + 50]
        try:
            data = _post_json(
                f"https://{host}/v2/translate",
                {"text": chunk, "source_lang": source_lang.upper(), "target_lang": target},
                {"Content-Type": "application/json", "Authorization": f"DeepL-Auth-Key {key}"},
                float(s["llm_timeout"]),
            )
        except _HTTPError as e:
            msg = {403: "DeepL rejected the API key", 456: "DeepL quota exceeded"}.get(e.status, e.detail)
            raise BackendError(f"DeepL error {e.status}: {msg}") from e
        out.extend(t["text"] for t in data["translations"])
        usage.record(s, provider="deepl", model="deepl", task="passage", chars=sum(len(t) for t in chunk))
    return out


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #


class _HTTPError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def _post_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise _HTTPError(e.code, detail) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BackendError(f"Could not reach {url}: {getattr(e, 'reason', e)}") from e
    except json.JSONDecodeError as e:
        raise BackendError(f"{url} did not return JSON") from e
