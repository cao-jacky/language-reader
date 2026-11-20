"""Flashcard scheduling with FSRS-4.5 (https://github.com/open-spaced-repetition).

Each word has a memory stability S (days until recall probability falls to
90%) and a difficulty D (1-10). A review with rating 1-4 (again, hard, good,
easy) updates both and schedules the next review after S days, which targets
90% retention. A failed card comes back after a minute, in the same session.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

# FSRS-4.5 default parameters.
W = (0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031, 1.6474, 0.1367,
     1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755)
DECAY = -0.5
FACTOR = 19 / 81  # so that R(S, S) = 0.9
RETENTION = 0.9
MAX_DAYS = 3650
RELEARN = timedelta(minutes=1)
FMT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse(ts: str) -> datetime:
    return datetime.strptime(ts, FMT).replace(tzinfo=timezone.utc)


def fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def _clamp_d(d: float) -> float:
    return min(max(d, 1.0), 10.0)


def _init_d(g: int) -> float:
    return _clamp_d(W[4] - (g - 3) * W[5])


def retrievability(elapsed_days: float, s: float) -> float:
    return (1 + FACTOR * elapsed_days / s) ** DECAY


def _interval(s: float) -> int:
    days = s / FACTOR * (RETENTION ** (1 / DECAY) - 1)
    return int(min(max(round(days), 1), MAX_DAYS))


def schedule(state: dict | None, rating: int, at: datetime | None = None) -> dict:
    """New state after a review. state: None for a new card, else a review_state row."""
    if rating not in (1, 2, 3, 4):
        raise ValueError("rating must be 1-4")
    at = at or now()
    if not state:
        s, d = W[rating - 1], _init_d(rating)
        reps, lapses = 0, 0
    else:
        s0, d0 = float(state["stability"]), float(state["difficulty"])
        elapsed = max((at - parse(state["last_review"])).total_seconds() / 86400, 0)
        r = retrievability(elapsed, s0)
        if rating == 1:
            s = min(W[11] * d0 ** -W[12] * ((s0 + 1) ** W[13] - 1) * math.exp((1 - r) * W[14]), s0)
        else:
            hard = W[15] if rating == 2 else 1
            easy = W[16] if rating == 4 else 1
            s = s0 * (1 + math.exp(W[8]) * (11 - d0) * s0 ** -W[9] * (math.exp((1 - r) * W[10]) - 1) * hard * easy)
        d = _clamp_d(W[7] * _init_d(4) + (1 - W[7]) * (d0 - W[6] * (rating - 3)))
        reps, lapses = int(state["reps"]), int(state["lapses"])
    s = max(s, 0.1)
    if rating == 1:
        due, days = at + RELEARN, 0.0
        lapses += 1 if state else 0
    else:
        days = _interval(s)
        due = at + timedelta(days=days)
    return {
        "due": fmt(due), "stability": s, "difficulty": d, "reps": reps + 1, "lapses": lapses,
        "last_review": fmt(at), "interval_days": days,
    }


def label(state: dict | None, rating: int, at: datetime | None = None) -> str:
    """What each rating button will do: '1m', '3d', '2mo', '1.5y'."""
    nxt = schedule(state, rating, at)
    days = nxt["interval_days"]
    if days == 0:
        return "1m"
    if days < 30:
        return f"{int(days)}d"
    if days < 365:
        return f"{round(days / 30)}mo"
    return f"{days / 365:.1f}y".replace(".0y", "y")
