"""Text utilities shared by processing, the pipeline stages and the simulated model: normalisation for the
verbatim passage check, language detection, sentence and paragraph splitting, shingles and quotations.

Language detection is by script (Arabic letters versus Latin letters) instead of langdetect: the core
slice accepts only Arabic and English, which script alone separates reliably, and it needs no model.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "«": '"', "»": '"'})
_WS = re.compile(r"\s+")
_ARABIC = re.compile(r"[؀-ۿ]")
_LATIN = re.compile(r"[A-Za-z]")
_SENT = re.compile(r"(?<=[.!?؟])\s+(?=[\"'A-Z؀-ۿ0-9])")
_WORD = re.compile(r"[\w؀-ۿ]+", re.UNICODE)
_QUOTE_SPAN = re.compile(r'"([^"]{20,400})"')


def normalise(text: str) -> str:
    """Form used for the verbatim passage check: NFKC, straight quotes, ASCII digits, single spaces,
    lower case. Whitespace, quote style and Arabic-Indic digits never make a real passage look invented."""
    t = unicodedata.normalize("NFKC", text).translate(ARABIC_DIGITS).translate(_QUOTES)
    return _WS.sub(" ", t).strip().lower()


def passage_in(passage: str, text: str) -> bool:
    p = normalise(passage)
    return bool(p) and p in normalise(text)


def detect_language(text: str) -> str:
    """'ar', 'en' or 'other' by the share of Arabic versus Latin letters."""
    ar, la = len(_ARABIC.findall(text)), len(_LATIN.findall(text))
    if ar + la < 10:
        return "other"
    if ar / (ar + la) >= 0.5:
        return "ar"
    return "en"


def content_hash(text: str) -> bytes:
    return hashlib.sha256(normalise(text).encode()).digest()


def sentences(text: str) -> list[str]:
    """Sentences; a line break also ends a sentence (headlines and list lines carry no full stop)."""
    out: list[str] = []
    for line in text.splitlines():
        out += [s.strip() for s in _SENT.split(_WS.sub(" ", line).strip()) if s.strip()]
    return out


def paragraphs(text: str, max_chars: int = 1200) -> list[str]:
    """Split on blank lines, then pack sentences so no paragraph is longer than max_chars."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        cur = ""
        for s in sentences(block):
            if cur and len(cur) + 1 + len(s) > max_chars:
                out.append(cur)
                cur = s
            else:
                cur = f"{cur} {s}".strip()
        if cur:
            out.append(cur)
    return out


def words(text: str) -> list[str]:
    return _WORD.findall(normalise(text))


def shingles(text: str, n: int = 5) -> set[tuple[str, ...]]:
    w = words(text)
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def quotations(text: str, min_words: int = 8) -> set[str]:
    """Direct quotations of at least min_words words, normalised (used to spot copies of one press release)."""
    t = text.translate(_QUOTES)
    return {normalise(q) for q in _QUOTE_SPAN.findall(t) if len(q.split()) >= min_words}


def numbers(text: str) -> list[str]:
    return re.findall(r"\d+(?:[.,]\d+)?", text.translate(ARABIC_DIGITS))
