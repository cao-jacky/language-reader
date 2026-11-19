"""Usage log and cost estimates for backend calls.

Every model call is recorded with its token counts and an estimated cost,
priced at the time of the call from the price list (built-in list prices,
overridden per model on the Settings page). Cache hits and DeepL characters
are recorded too, so the Settings page can show what was spent and saved.
These are estimates; the provider's own bill is authoritative.
"""
from __future__ import annotations

import json

from . import db

# USD per million tokens (input, output): Anthropic list prices.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-fable-5-2": (8.00, 40.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

DEEPL_FREE_CHARS = 500_000


def prices(saved_json: str) -> dict[str, dict]:
    """Built-in prices overlaid with the ones saved in settings."""
    out = {m: {"in": p[0], "out": p[1], "source": "list price"} for m, p in DEFAULT_PRICES.items()}
    try:
        saved = json.loads(saved_json or "{}")
    except json.JSONDecodeError:
        saved = {}
    for model, p in saved.items():
        try:
            out[model] = {"in": float(p["in"]), "out": float(p["out"]), "source": "custom"}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def validate_prices(value: str) -> str:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError as e:
        raise ValueError("Prices must be JSON") from e
    if not isinstance(data, dict):
        raise ValueError("Prices must be an object keyed by model")
    clean = {}
    for model, p in data.items():
        try:
            pin, pout = float(p["in"]), float(p["out"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Price for {model} needs numeric in and out") from e
        if pin < 0 or pout < 0:
            raise ValueError(f"Price for {model} can't be negative")
        clean[str(model).strip()] = {"in": pin, "out": pout}
    return json.dumps(clean, sort_keys=True)


def record(s: dict, *, provider: str, model: str, task: str, input_tokens: int | None = None,
           output_tokens: int | None = None, chars: int = 0, cached: bool = False) -> None:
    cost = None
    if not cached and provider != "deepl" and input_tokens is not None and output_tokens is not None:
        p = prices(s.get("llm_prices", "")).get(model)
        if p:
            cost = (input_tokens * p["in"] + output_tokens * p["out"]) / 1_000_000
    try:
        with db.tx() as c:
            c.execute(
                "INSERT INTO usage(provider, model, task, input_tokens, output_tokens, chars, cached, cost_usd) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (provider, model, task, input_tokens, output_tokens, chars, int(cached), cost),
            )
    except Exception:  # noqa: BLE001 - never fail a lookup because logging failed
        pass


def summary() -> dict:
    month = "strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')"
    with db.tx() as c:
        def totals(where: str) -> dict:
            r = c.execute(
                f"""SELECT COUNT(*) FILTER (WHERE NOT cached AND provider <> 'deepl') AS requests,
                           COALESCE(SUM(input_tokens), 0) AS input_tokens,
                           COALESCE(SUM(output_tokens), 0) AS output_tokens,
                           COALESCE(SUM(cost_usd), 0) AS cost,
                           COUNT(*) FILTER (WHERE NOT cached AND provider <> 'deepl' AND cost_usd IS NULL) AS unpriced,
                           COUNT(*) FILTER (WHERE cached) AS cache_hits,
                           COALESCE(SUM(chars) FILTER (WHERE provider = 'deepl' AND NOT cached), 0) AS deepl_chars
                    FROM usage WHERE {where}"""
            ).fetchone()
            return dict(r)

        by_model = [
            dict(r)
            for r in c.execute(
                f"""SELECT model, provider, COUNT(*) AS requests,
                           COALESCE(SUM(input_tokens), 0) AS input_tokens,
                           COALESCE(SUM(output_tokens), 0) AS output_tokens,
                           SUM(cost_usd) AS cost
                    FROM usage WHERE NOT cached AND provider <> 'deepl' AND {month}
                    GROUP BY model, provider ORDER BY COALESCE(SUM(cost_usd), 0) DESC, requests DESC"""
            )
        ]
        daily = [
            dict(r)
            for r in c.execute(
                """SELECT date(created_at) AS day, COALESCE(SUM(cost_usd), 0) AS cost,
                          COUNT(*) FILTER (WHERE NOT cached AND provider <> 'deepl') AS requests
                   FROM usage WHERE created_at >= datetime('now', '-29 days')
                   GROUP BY day ORDER BY day"""
            )
        ]
        by_task = [
            dict(r)
            for r in c.execute(
                f"""SELECT task, COUNT(*) AS requests, COALESCE(SUM(cost_usd), 0) AS cost
                    FROM usage WHERE NOT cached AND provider <> 'deepl' AND {month}
                    GROUP BY task ORDER BY cost DESC"""
            )
        ]
        return {
            "month": totals(month),
            "all_time": totals("1"),
            "by_model": by_model,
            "by_task": by_task,
            "daily": daily,
            "deepl_free_chars": DEEPL_FREE_CHARS,
        }


def reset() -> None:
    with db.tx() as c:
        c.execute("DELETE FROM usage")
