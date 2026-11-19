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


_FI_WORDS = frozenset(
    "ja on ei se että kun mutta niin oli ovat myös tai joka jos hän minä sinä me te he olen ole "
    "olla tämä tuo nyt vain kuin sitten jo vielä koska mitä mikä kanssa olisi voi pitää eikä siis "
    "sen sitä hänen".split()
)


def detect_lang(text: str) -> str | None:
    """Return 'ru', 'fi', or None when the text is neither (or too short to tell).

    Russian is recognised by script, ruling out Ukrainian and Belarusian by
    their own letters. Finnish is recognised by common function words or a high
    rate of ä/ö without the letters that mark German, Swedish or Estonian.
    The same rules run in the browser for live feedback.
    """
    letters = [c for c in text if c.isalpha()]
    n = len(letters)
    cyr = sum("Ѐ" <= c <= "ӿ" for c in letters)
    if cyr >= 3 and cyr / n > 0.5:
        other = sum(c in "іїєґўІЇЄҐЎ" for c in letters)
        return None if other / n > 0.01 else "ru"
    if n < 15:
        return None
    lower = text.lower()
    words = re.findall(r"[^\W\d_]+", lower)
    if not words:
        return None
    hits = sum(w in _FI_WORDS for w in words) / len(words)
    ao = sum(c in "äö" for c in lower) / n
    # Finnish uses b c f q w x z only in loanwords; English, German and
    # Swedish use them constantly. ü ß å õ mark German, Swedish and Estonian.
    foreign = sum(c in "bcfqwxzüßåõ" for c in lower) / n
    if foreign >= 0.025:
        return None
    return "fi" if hits >= 0.06 or ao >= 0.02 else None


def default_title(body: str, limit: int = 70) -> str:
    """First sentence, cut at a word boundary."""
    spans = split_sentences(body)
    first = body[spans[0][0] : spans[0][1]] if spans else body.strip()
    first = first.split("\n", 1)[0].strip()
    if len(first) <= limit:
        return first
    cut = first[:limit].rsplit(" ", 1)[0].rstrip(",;:—–- ")
    return (cut or first[:limit]) + "…"


def tokens(body: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
    end = len(body) if end is None else end
    return [(m.start(), m.end()) for m in WORD_RE.finditer(body, start, end)]
