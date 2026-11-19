"""Sentence splitting and word tokenisation for Russian and Finnish text.

Everything works on character offsets into the original body, so the stored
text is never rewritten.
"""
from __future__ import annotations

import re

STRESS_MARKS = "́̀"

# Letters (any script), allowing combining stress marks inside Russian words and
# hyphen/apostrophe joins (что-то, linja-auto, vaa'an).
_LETTER = r"(?:[^\W\d_]|[́̀])"
WORD_RE = re.compile(rf"{_LETTER}+(?:[-'’]{_LETTER}+)*")

# A sentence ends at terminal punctuation (plus closing quotes/brackets) that is
# followed by whitespace, or at a line break.
_END_RE = re.compile(r"[.!?…]+[»\"”’)\]]*(?=\s)|\n")


def strip_stress(s: str) -> str:
    return "".join(ch for ch in s if ch not in STRESS_MARKS)


def split_sentences(body: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _END_RE.finditer(body):
        if m.group() != "\n" and _next_is_lower(body, m.end()):
            # "т. е. мы…", "esim. jos…": an abbreviation, not a sentence end.
            continue
        _add(spans, body, pos, m.end())
        pos = m.end()
    _add(spans, body, pos, len(body))
    return spans


def _next_is_lower(body: str, i: int) -> bool:
    while i < len(body) and body[i] in " \t":
        i += 1
    return i < len(body) and body[i].islower()


def _add(spans: list[tuple[int, int]], body: str, s: int, e: int) -> None:
    while s < e and body[s].isspace():
        s += 1
    while e > s and body[e - 1].isspace():
        e -= 1
    if s < e:
        spans.append((s, e))


def tokens(body: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
    end = len(body) if end is None else end
    return [(m.start(), m.end()) for m in WORD_RE.finditer(body, start, end)]
