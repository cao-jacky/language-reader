"""Sentence splitting and word tokenisation.

Everything works on character offsets into the stored body, so tokens map
straight back onto the text the reader shows.
"""
from __future__ import annotations

import re
import unicodedata

STRESS_MARKS = "́̀"

# Characters that join two word parts into one word: hyphens (что-то,
# linja-auto, 1990-luvulla), apostrophes (vaa'an), the soft hyphen that web
# pages use for line breaking, and the zero-width (non-)joiners of Persian
# and Indic scripts.
_JOINERS = frozenset("-'’­‐‑‌‍")

# A sentence ends at terminal punctuation (plus closing quotes/brackets) that is
# followed by whitespace, or at a line break.
_END_RE = re.compile(r"[.!?…]+[»\"”’)\]]*(?=\s)|[。！？]+[」』”’）]*|[।॥]|\n")


def normalise(text: str) -> str:
    """NFC, so ä/й/ё are one character each rather than a letter plus a mark.

    Text copied from PDFs and some macOS apps arrives decomposed; storing it
    composed keeps searching and matching of word forms reliable. Soft hyphens
    (invisible line-break hints from web pages) are dropped for the same reason.
    """
    return unicodedata.normalize("NFC", text).replace("­", "")


def strip_stress(s: str) -> str:
    return "".join(ch for ch in s if ch not in STRESS_MARKS)


def _kind(ch: str) -> str | None:
    o = ord(ch)
    # Chinese characters are clickable one at a time (no spaces to split on);
    # runs of Japanese kana stay together.
    if 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF:
        return "han"
    if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
        return "kana"
    cat = unicodedata.category(ch)
    if cat[0] == "L":
        return "letter"
    if cat[0] == "M":  # combining accents, stress marks, Indic vowel signs
        return "mark"
    if cat == "Nd":
        return "digit"
    return None


def tokens(body: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
    """Clickable words in body[start:end], as (start, end) offsets.

    A word is a run of letters, combining marks and digits, joined across
    hyphens, apostrophes and soft hyphens, and across a colon when letters
    follow it (Finnish EU:n, USA:ssa). Runs with no letter at all (plain
    numbers) are not words.
    """
    end = len(body) if end is None else end
    out: list[tuple[int, int]] = []
    i = start
    while i < end:
        k = _kind(body[i])
        if k == "han":
            out.append((i, i + 1))
            i += 1
            continue
        if k == "kana":
            j = i + 1
            while j < end and _kind(body[j]) in ("kana", "mark"):
                j += 1
            out.append((i, j))
            i = j
            continue
        if k not in ("letter", "mark", "digit"):
            i += 1
            continue
        j = i
        has_letter = False
        while j < end:
            kj = _kind(body[j])
            if kj in ("letter", "mark", "digit"):
                has_letter = has_letter or kj == "letter"
                j += 1
                continue
            nxt = _kind(body[j + 1]) if j + 1 < end else None
            ch = body[j]
            if ch in _JOINERS and nxt in ("letter", "mark", "digit"):
                j += 1
                continue
            if ch == ":" and nxt == "letter" and j > i:
                j += 1
                continue
            break
        if has_letter:
            out.append((i, j))
        i = j
    return out


def split_sentences(body: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _END_RE.finditer(body):
        if m.group()[0] in ".!?…" and _next_is_lower(body, m.end()):
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


def default_title(body: str, limit: int = 70) -> str:
    """First sentence, cut at a word boundary."""
    spans = split_sentences(body)
    first = body[spans[0][0] : spans[0][1]] if spans else body.strip()
    first = first.split("\n", 1)[0].strip()
    if len(first) <= limit:
        return first
    cut = first[:limit].rsplit(" ", 1)[0].rstrip(",;:—–- ")
    return (cut or first[:limit]) + "…"
