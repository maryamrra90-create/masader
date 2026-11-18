"""JSON schemas of every role's output (architecture C.6). Each is sent to the provider as the structured
output format and validated again after the call; additionalProperties is false everywhere, and labels
and verdicts are enums, so a model cannot invent a value. SCHEMA_VERSION must match the version each
prompt file declares (the gateway refuses a mismatch)."""
from __future__ import annotations

SCHEMA_VERSION = "1"


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required if required is not None else list(props),
            "additionalProperties": False}


_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}
_CITES = {"type": "array", "items": {"type": "integer"}}
_SENTENCE = _obj({"text": _STR, "cites": _CITES})

SCHEMAS: dict[str, dict] = {
    "planner": _obj({
        "too_broad": {"type": "boolean"},
        "ambiguous_entities": {"type": "array", "items": _obj({"name": _STR, "candidates": _STRS})},
        "questions": {"type": "array", "maxItems": 3,
                      "items": _obj({"id": _STR, "text": _STR, "default": _STR, "options": _STRS})},
        "queries_en": _STRS,
        "queries_ar": _STRS,
    }),
    "extractor": _obj({"claims": {"type": "array", "maxItems": 8, "items": _obj({
        "text": _STR,
        "type": {"enum": ["fact", "attributed", "opinion", "forecast", "other"]},
        "speaker": {"type": ["string", "null"]},
        "numbers": _STRS, "dates": _STRS, "entities": _STRS,
        "item_id": {"type": "integer"},
        "passage": _STR,
    })}}),
    "verifier": _obj({"results": {"type": "array", "items": _obj({
        "claim_id": {"type": "integer"}, "item_id": {"type": "integer"},
        "verdict": {"enum": ["supports", "contradicts", "not_addressed"]},
        "reason": _STR, "first_hand": {"type": "boolean"},
    })}}),
    "writer": _obj({"sections": _obj({
        "key_findings": {"type": "array", "items": _SENTENCE},
        "by_the_numbers": {"type": "array", "items": _SENTENCE},
        "disputed": {"type": "array", "items": _SENTENCE},
        "actors": {"type": "array", "items": _SENTENCE},
        "analysis": {"type": "array", "items": _SENTENCE},
        "what_to_watch": {"type": "array", "items": _SENTENCE},
    })}),
    "splitter": _obj({"assertions": {"type": "array", "items": _obj({
        "n": {"type": "integer"}, "section": _STR, "text": _STR, "cites": _CITES,
        "speaker": {"type": ["string", "null"]},
    })}}),
    "support": _obj({"results": {"type": "array", "items": _obj({
        "n": {"type": "integer"}, "result": {"enum": ["pass", "fail"]}, "reason": _STR,
    })}}),
}
