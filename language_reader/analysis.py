"""The three language tasks: analyse a word, translate a phrase, translate
sentences. Each result is cached in SQLite by backend, model and input."""
from __future__ import annotations

import hashlib
import json

from . import db, languages, llm, morph, usage
CEFR = ["A1", "A2", "B1", "B2", "C1", "C2"]

WORD_SCHEMA = {
    "type": "object",
    "properties": {
        "lemma": {"type": "string"},
        "stressed": {"type": "string"},
        "pos": {"type": "string"},
        "morphology": {"type": "string"},
        "gloss": {"type": "string"},
        "other_meanings": {"type": "array", "items": {"type": "string"}},
        "compound_parts": {"type": "array", "items": {"type": "string"}},
        "cefr": {"type": "string", "enum": CEFR},
        "notes": {"type": "string"},
    },
    "required": [
        "lemma", "stressed", "pos", "morphology", "gloss",
        "other_meanings", "compound_parts", "cefr", "notes",
    ],
    "additionalProperties": False,
}

PHRASE_SCHEMA = {
    "type": "object",
    "properties": {
        "translation": {"type": "string"},
        "literal": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["translation", "literal", "notes"],
    "additionalProperties": False,
}

SENTENCES_SCHEMA = {
    "type": "object",
    "properties": {"translations": {"type": "array", "items": {"type": "string"}}},
    "required": ["translations"],
    "additionalProperties": False,
}

GENERIC_GUIDANCE = (
    "In `stressed`, give the lemma with whatever pronunciation aid a learner of this language "
    "would want: stress marks for languages with unpredictable stress (Ukrainian, Bulgarian), "
    "pinyin with tone marks for Chinese, the kana reading for Japanese words written in kanji, "
    "vowel points or a transliteration for scripts that omit vowels; otherwise repeat the lemma. "
    "Mention notable irregularities, gender, or verb class in `notes`. Use `compound_parts` only "
    "for compound words."
)

LANG_GUIDANCE = {
    "ru": (
        "Russian specifics: in `stressed`, write the lemma with an acute accent (U+0301) "
        "after the stressed vowel (ё needs none). For verbs, give the aspect and its "
        "aspectual partner in `notes` (e.g. 'imperfective; perfective: сделать'). For nouns, "
        "give gender in `morphology`. `compound_parts` is usually empty."
    ),
    "fi": (
        "Finnish specifics: `stressed` is the same as `lemma`. In `morphology`, name the case in "
        "English and Finnish with its ending (e.g. 'inessive (-ssa), singular'), plus any "
        "possessive suffix or clitic (-kin, -ko, -han). For verbs, give the verb type (1–6) and "
        "any consonant gradation in `notes`. For compounds, list the parts in `compound_parts` "
        "(e.g. ['rauta', 'tie', 'asema'])."
    ),
}


def _system(lang: str, target: str) -> str:
    return (
        f"You are a precise linguistics assistant for an adult learning {languages.name(lang)}. "
        f"The learner's working language is {target}: write every gloss, translation and "
        f"note in {target}. Be accurate and concise. Reply with a single JSON object that "
        f"matches the requested schema."
    )


def _cached(kind: str, s: dict, payload, fn):
    raw = json.dumps(
        [kind, s["llm_provider"], s["llm_model"], s["llm_base_url"], s["target_language"], payload],
        ensure_ascii=False,
        sort_keys=True,
    )
    key = hashlib.sha256(raw.encode()).hexdigest()
    with db.tx() as c:
        row = c.execute("SELECT value FROM llm_cache WHERE key = ?", (key,)).fetchone()
    if row:
        deepl = kind == "deepl"
        usage.record(s, provider="deepl" if deepl else s["llm_provider"], model="deepl" if deepl else s["llm_model"],
                     task={"sentences": "passage", "deepl": "passage"}.get(kind, kind), cached=True)
        return json.loads(row["value"])
    value = fn()
    with db.tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO llm_cache(key, value) VALUES(?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
    return value


def analyse_word(s: dict, lang: str, sentence: str, rel_start: int, rel_end: int, use_cache: bool = True) -> dict:
    word = sentence[rel_start:rel_end]
    marked = f"{sentence[:rel_start]}⟦{word}⟧{sentence[rel_end:]}"
    hints = morph.candidates(lang, word)
    hint_text = (
        "Offline analyser candidates (may be ambiguous; pick the one that fits the sentence, "
        "or ignore them if none fit):\n- " + "\n- ".join(hints)
        if hints
        else ""
    )
    user = f"""Sentence: {marked}
Marked word: {word}
{hint_text}

Analyse the marked word as it is used in this sentence.
- lemma: its dictionary form, without stress marks.
- stressed: the lemma with stress marked (see below).
- pos: part of speech in lowercase English (noun, verb, adjective, adverb, pronoun, preposition, conjunction, particle, numeral, interjection, proper noun).
- morphology: the grammatical form of the marked word in this sentence.
- gloss: its meaning in this sentence, 1–6 words.
- other_meanings: up to 4 other common meanings of the lemma (may be empty).
- compound_parts: parts of a compound word, otherwise empty.
- cefr: the CEFR level (A1–C2) at which a learner typically meets this lemma, judged by frequency and standard curricula (for example TORFL lexical minimums for Russian, YKI levels for Finnish, HSK for Chinese).
- notes: one or two short sentences on anything a learner should know (irregular forms, government, idiomatic use); empty if nothing notable.

{LANG_GUIDANCE.get(lang, GENERIC_GUIDANCE)}"""

    def run():
        return llm.complete_json(s, _system(lang, s["target_language"]), user, WORD_SCHEMA, 2000, task="word")

    if not use_cache:
        return run()
    return _cached("word", s, [lang, marked], run)


def translate_phrase(s: dict, lang: str, phrase: str, context: str) -> dict:
    user = f"""Context: {context}
Phrase: {phrase}

Translate the phrase as it is used in this context.
- translation: a natural {s['target_language']} translation.
- literal: a word-for-word rendering if it differs meaningfully from the translation, otherwise empty.
- notes: explain any idiom, collocation or grammar point a learner should notice, in one or two short sentences; empty if nothing notable."""

    return _cached(
        "phrase",
        s,
        [lang, phrase, context],
        lambda: llm.complete_json(s, _system(lang, s["target_language"]), user, PHRASE_SCHEMA, 1500, task="phrase"),
    )


def translate_sentences(s: dict, lang: str, sentences: list[str]) -> list[str]:
    if s["passage_provider"] == "deepl":
        return _cached("deepl", s, [lang, sentences], lambda: llm.deepl_translate(s, sentences, lang))
    out: list[str] = []
    for i in range(0, len(sentences), 25):
        out.extend(_translate_chunk(s, lang, sentences[i : i + 25]))
    return out


def _translate_chunk(s: dict, lang: str, chunk: list[str]) -> list[str]:
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(chunk))
    user = f"""Translate each numbered {languages.name(lang)} sentence into natural {s['target_language']}. The sentences are consecutive, so use the surrounding ones for context.

{numbered}

Return `translations` with exactly {len(chunk)} strings, in the same order, without the numbers."""

    def run():
        data = llm.complete_json(
            s, _system(lang, s["target_language"]), user, SENTENCES_SCHEMA, 400 + 250 * len(chunk), task="passage"
        )
        tr = data["translations"]
        if len(tr) != len(chunk):
            if len(chunk) == 1:
                return [" ".join(tr)]
            # The model merged or split sentences: fall back to one call each.
            return [t for sent in chunk for t in _translate_chunk(s, lang, [sent])]
        return tr

    return _cached("sentences", s, [lang, chunk], run)


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "key_points"],
    "additionalProperties": False,
}


def summarise(s: dict, lang: str, title: str, body: str, kind: str) -> dict:
    """kind 'target': summary in the learner's language. 'simple': a summary in
    the text's own language, in plain words a B1 learner can read."""
    name = languages.name(lang)
    if kind == "simple":
        how = (f"Write the summary and key points in {name}, not in {s['target_language']}, using short "
               f"sentences and common words a B1-level learner of {name} can follow.")
    else:
        how = f"Write the summary and key points in {s['target_language']}."
    user = f"""Title: {title}

Text:
{body[:60000]}

Summarise this {name} text.
- summary: 2–4 sentences on what the text says.
- key_points: up to 5 short key points (may be empty for a very short text).
{how}"""
    return _cached(
        "summary",
        s,
        [lang, kind, title, body],
        lambda: llm.complete_json(s, _system(lang, s["target_language"]), user, SUMMARY_SCHEMA, 1500, task="summary"),
    )
