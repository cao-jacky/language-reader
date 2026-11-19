"""The languages the reader works with, and detecting which one a text is in.

The list of languages being learned is a setting (comma-separated ISO 639
codes). Detection uses py3langid, an offline classifier, and only ever answers
with one of the configured languages; when the text looks like something else
it says what, so the page can suggest adding it.
"""
from __future__ import annotations

import threading
import unicodedata

# Languages py3langid can identify, by English name. Codes it knows but that
# are not listed here (dialects, historical forms) are not offered.
NAMES = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "as": "Assamese", "az": "Azerbaijani",
    "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian", "bn": "Bengali", "br": "Breton",
    "bs": "Bosnian", "ca": "Catalan", "cs": "Czech", "cy": "Welsh", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "eo": "Esperanto", "es": "Spanish", "et": "Estonian",
    "eu": "Basque", "fa": "Persian", "fi": "Finnish", "fo": "Faroese", "fr": "French",
    "fy": "Frisian", "ga": "Irish", "gd": "Scottish Gaelic", "gl": "Galician", "gu": "Gujarati",
    "ha": "Hausa", "he": "Hebrew", "hi": "Hindi", "hr": "Croatian", "ht": "Haitian Creole",
    "hu": "Hungarian", "hy": "Armenian", "id": "Indonesian", "ig": "Igbo", "is": "Icelandic",
    "it": "Italian", "ja": "Japanese", "jv": "Javanese", "ka": "Georgian", "kk": "Kazakh",
    "km": "Khmer", "kn": "Kannada", "ko": "Korean", "ku": "Kurdish", "ky": "Kyrgyz", "la": "Latin",
    "lb": "Luxembourgish", "lo": "Lao", "lt": "Lithuanian", "lv": "Latvian", "mg": "Malagasy",
    "mk": "Macedonian", "ml": "Malayalam", "mn": "Mongolian", "mr": "Marathi", "ms": "Malay",
    "mt": "Maltese", "my": "Burmese", "ne": "Nepali", "nl": "Dutch", "nn": "Norwegian Nynorsk",
    "no": "Norwegian", "oc": "Occitan", "or": "Odia", "pa": "Punjabi", "pl": "Polish",
    "ps": "Pashto", "pt": "Portuguese", "qu": "Quechua", "ro": "Romanian", "ru": "Russian",
    "rw": "Kinyarwanda", "sa": "Sanskrit", "se": "Northern Sami", "si": "Sinhala", "sk": "Slovak",
    "sl": "Slovenian", "sn": "Shona", "so": "Somali", "sq": "Albanian", "sr": "Serbian",
    "sv": "Swedish", "sw": "Swahili", "ta": "Tamil", "te": "Telugu", "tg": "Tajik", "th": "Thai",
    "tk": "Turkmen", "tl": "Tagalog", "tr": "Turkish", "tt": "Tatar", "ug": "Uyghur",
    "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek", "vi": "Vietnamese", "xh": "Xhosa",
    "yo": "Yoruba", "zh": "Chinese", "zu": "Zulu",
}

DEFAULT = "ru,fi"


def name(code: str) -> str:
    return NAMES.get(code, code)


def parse(value: str) -> list[str]:
    out: list[str] = []
    for c in (value or "").split(","):
        c = c.strip().lower()
        if c in NAMES and c not in out:
            out.append(c)
    return out


def validate(value: str) -> str:
    raw = [c.strip().lower() for c in (value or "").split(",") if c.strip()]
    unknown = [c for c in raw if c not in NAMES]
    if unknown:
        raise ValueError("Unknown language code: " + ", ".join(unknown))
    codes = parse(value)
    if not codes:
        raise ValueError("Keep at least one language")
    return ",".join(codes)


def describe(codes: list[str]) -> list[dict]:
    return [{"code": c, "name": name(c)} for c in codes]


def available() -> list[dict]:
    return sorted(describe(list(NAMES)), key=lambda d: d["name"])


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_ident = None


def _identifier():
    global _ident
    with _lock:
        if _ident is None:
            from py3langid.langid import MODEL_FILE, LanguageIdentifier

            _ident = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)
    return _ident


def detect(text: str, configured: list[str]) -> dict:
    """Pick which configured language `text` is in.

    Returns {"lang": code or None, "guess": code or None, "confidence": float}.
    `guess` is the classifier's overall best answer, which may be a language
    not in `configured` (so the caller can say "looks like English").
    """
    sample = unicodedata.normalize("NFC", text.strip()[:4000])
    letters = sum(ch.isalpha() for ch in sample)
    if letters < 3:
        return {"lang": None, "guess": None, "confidence": 0.0}
    ranked = [(code, float(p)) for code, p in _identifier().rank(sample)]
    top_code, top_p = ranked[0]
    guess = top_code if top_code in NAMES else None
    if top_code in configured and top_p >= 0.4:
        return {"lang": top_code, "guess": guess, "confidence": round(top_p, 3)}
    # Short or closely related texts (Russian vs Bulgarian, say) can rank a
    # sibling first; accept a configured language that is a close second.
    for code, p in ranked:
        if code in configured:
            if p >= 0.15 and p >= top_p * 0.5:
                return {"lang": code, "guess": guess, "confidence": round(p, 3)}
            break
    return {"lang": None, "guess": guess if letters >= 12 else None, "confidence": round(top_p, 3)}
