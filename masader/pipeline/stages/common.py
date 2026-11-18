"""Helpers the AI stages share: loading items with their source data, picking the passages to send."""
from __future__ import annotations

from ...processing.text import paragraphs
from ...simulated.heuristics import content_words
from ...store.db import pool


def load_items(ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    with pool().connection() as c:
        rows = c.execute(
            "SELECT i.id, i.url, i.title, coalesce(i.full_text, i.excerpt) AS text, i.published_at, i.lang, "
            "i.origin_group_id, i.embedding::text AS embedding, s.outlet_name, s.tier::text AS tier, s.official, s.code "
            "FROM items i JOIN sources s ON s.id=i.source_id WHERE i.id = ANY(%s)", (ids,)).fetchall()
    return {r["id"]: dict(r) for r in rows}


def full_text(item: dict) -> str:
    return f"{item['title']}\n\n{item['text']}"


def select_passages(item: dict, focus: str, n: int, max_chars: int) -> str:
    """The n paragraphs of the item that share most words with `focus`, in document order."""
    paras = paragraphs(full_text(item), max_chars)
    if len(paras) <= n:
        return "\n\n".join(paras)
    fw = content_words(focus)
    ranked = sorted(range(len(paras)), key=lambda i: (-len(content_words(paras[i]) & fw), i))[:n]
    return "\n\n".join(paras[i] for i in sorted(ranked))


def source_block(item: dict, text: str) -> dict:
    return {"id": item["id"], "outlet": item["outlet_name"], "tier": item["tier"],
            "published": item["published_at"].isoformat(), "text": text}


def chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]
