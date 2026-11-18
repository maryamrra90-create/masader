"""Input policy and PII redaction (proposal A17, NFR5, tests U6 and U12).

Runs before anything is stored or sent to a model:
  * `check_policy` rejects requests that the input policy forbids (confidential markings, credentials,
    profiling a private individual) and returns the reason;
  * `redact` replaces personal identifiers with typed placeholders and reports what was redacted.

This is a set of pattern recognizers, the same kind the architecture planned to use from Presidio
("pattern recognizers only", B.3.2). Writing them directly avoids pulling spaCy and Presidio into the
image for regular expressions; the cost is that names and street addresses are not detected (Presidio's
pattern-only mode would not detect them either).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_EMIRATES_ID = re.compile(r"\b784[- ]?\d{4}[- ]?\d{7}[- ]?\d\b")
_PASSPORT = re.compile(r"\b(?:passport(?: number| no\.?)?[:\s#]*)([A-Z]{1,2}\d{6,8})\b", re.I)
_PHONE = re.compile(r"(?<![\w.])(?:\+|00)\d{1,3}[\s-]?(?:\(?\d{1,4}\)?[\s-]?){2,4}\d{2,4}(?![\w.])")
_LOCAL_PHONE = re.compile(r"(?<![\w.])0\d{1,2}[\s-]?\d{3}[\s-]?\d{4}(?![\w.])")

# Markings, not ordinary words: "restricted shipping lanes" is a normal topic, "RESTRICTED" or
# "classification: restricted" is a marking. Arabic markers must stand alone ("سري" is inside "سريع").
_CONFIDENTIAL = re.compile(
    r"\b(?:CONFIDENTIAL|RESTRICTED|CLASSIFIED|TOP SECRET|SECRET//\w+)\b"
    r"|(?i:\b(?:internal use only|for official use only|do not distribute|marked confidential|"
    r"classification\s*:\s*(?:confidential|restricted|secret|classified))\b)"
    r"|(?<![\u0600-\u06FF])(?:سري|محظور|سري للغاية)(?![\u0600-\u06FF])")
_SECRET = re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|"
                     r"password\s*[:=]\s*\S+|api[_ -]?key\s*[:=]\s*\S+)", re.I)
_PROFILING = re.compile(r"\b(home address|where (?:does|do) .{1,40} live|track (?:the )?(?:location|movements)|"
                        r"locate (?:a |the )?(?:person|individual)|phone number of|daily routine of)\b", re.I)


@dataclass
class Redaction:
    text: str
    found: list[str] = field(default_factory=list)   # placeholder types, in order of appearance


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def redact(text: str) -> Redaction:
    """Replace personal identifiers with [EMAIL], [PHONE], [ID_NUMBER], [PASSPORT], [IBAN], [CARD]."""
    found: list[str] = []

    def sub(pattern: re.Pattern, label: str, s: str, check=None, group: int = 0) -> str:
        def repl(m: re.Match) -> str:
            value = m.group(group)
            if check and not check(value):
                return m.group(0)
            found.append(label)
            return m.group(0).replace(value, f"[{label}]") if group else f"[{label}]"
        return pattern.sub(repl, s)

    t = sub(_EMAIL, "EMAIL", text)
    t = sub(_EMIRATES_ID, "ID_NUMBER", t)
    t = sub(_IBAN, "IBAN", t)
    t = sub(_CARD, "CARD", t, check=lambda v: _luhn(re.sub(r"\D", "", v)))
    t = sub(_PASSPORT, "PASSPORT", t, group=1)
    t = sub(_PHONE, "PHONE", t)
    t = sub(_LOCAL_PHONE, "PHONE", t)
    return Redaction(t, found)


def check_policy(text: str) -> str | None:
    """Return why the input is refused, or None if the input policy allows it."""
    if _SECRET.search(text):
        return "the request contains what looks like a credential or secret"
    if _CONFIDENTIAL.search(text):
        return "the request contains material marked confidential, internal, restricted or classified"
    if _PROFILING.search(text):
        return "requests to profile, track or locate a private individual are not accepted"
    return None
