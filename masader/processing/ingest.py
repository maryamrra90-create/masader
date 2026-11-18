"""Ingest: source register sync, idempotent item upsert with embeddings, and origin-group assignment
(architecture D.2 steps 1-4, FR4-FR5).

Items are upserted by (source_id, url_hash), so running a connector twice stores nothing new. After new
items arrive, `assign_groups` re-runs the union-find grouping over the day window around them and merges
the result into the stored origin groups: an item keeps its group, and two stored groups that the new
evidence joins are merged into the older one.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass

import numpy as np

from .. import config
from ..ids import url_hash
from ..store.db import pool
from ..store.objectstore import get_store
from .embed import get_embedder, to_pgvector
from .origin_groups import ItemView, group
from .text import content_hash, detect_language

log = logging.getLogger("masader.ingest")


@dataclass
class RawItem:
    source_code: str
    url: str
    title: str
    text: str
    published: dt.datetime
    lang: str | None = None


def sync_sources() -> dict[str, int]:
    """Mirror config/sources.yaml into the sources table; returns {code: id}."""
    version = config.config_hash()
    ids = {}
    with pool().connection() as c, c.transaction():
        for s in config.sources():
            row = c.execute(
                "INSERT INTO sources (code, kind, outlet_name, outlet_domain, language, tier, tier_reason, register_version, "
                "allow_fulltext, active, official) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (code) DO UPDATE SET kind=EXCLUDED.kind, outlet_name=EXCLUDED.outlet_name, "
                "outlet_domain=EXCLUDED.outlet_domain, language=EXCLUDED.language, tier=EXCLUDED.tier, "
                "tier_reason=EXCLUDED.tier_reason, register_version=EXCLUDED.register_version, "
                "allow_fulltext=EXCLUDED.allow_fulltext, active=EXCLUDED.active, official=EXCLUDED.official RETURNING id",
                (s["code"], s["kind"], s["outlet_name"], s.get("outlet_domain"), s["language"], s["tier"], s["tier_reason"],
                 version, bool(s.get("allow_fulltext")), bool(s.get("active")), bool(s.get("official")))).fetchone()
            ids[s["code"]] = row["id"]
    return ids


def _aliases() -> dict[str, list[str]]:
    return {s["outlet_name"]: s.get("aliases", []) for s in config.sources()}


def ingest(items: list[RawItem], store_raw: bool = True) -> list[int]:
    """Upsert items; returns the ids of the items that were new. Origin groups are assigned afterwards."""
    if not items:
        return []
    src = {s["code"]: s for s in config.sources()}
    with pool().connection() as c:
        ids = {r["code"]: (r["id"], r["allow_fulltext"]) for r in c.execute("SELECT id, code, allow_fulltext FROM sources")}
    vecs = get_embedder().embed([f"{i.title}\n{i.text}" for i in items])
    store = get_store() if store_raw else None
    new_ids = []
    with pool().connection() as c, c.transaction():
        for it, vec in zip(items, vecs):
            if it.source_code not in ids or it.source_code not in src:
                log.warning("item from unregistered source %s skipped", it.source_code)
                continue
            sid, fulltext = ids[it.source_code]
            uh = url_hash(it.url)
            key = None
            if store is not None:
                key = f"raw/{it.source_code}/{it.published:%Y-%m-%d}/{uh.hex()[:16]}.json"
                store.put(key, json.dumps({"url": it.url, "title": it.title, "text": it.text,
                                           "published": it.published.isoformat()}, ensure_ascii=False).encode(), "application/json")
            lang = it.lang or detect_language(it.title + " " + it.text)
            row = c.execute(
                "INSERT INTO items (source_id, url, url_hash, content_hash, title, excerpt, full_text, lang, published_at, "
                "raw_object_key, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector) "
                "ON CONFLICT (source_id, url_hash) DO NOTHING RETURNING id",
                (sid, it.url, uh, content_hash(it.title + "\n" + it.text), it.title, it.text[:600],
                 it.text if fulltext else None, lang if lang in ("ar", "en") else "en", it.published, key, to_pgvector(vec))).fetchone()
            if row:
                new_ids.append(row["id"])
    if new_ids:
        assign_groups(new_ids)
    return new_ids


def _views(rows) -> list[ItemView]:
    aliases = _aliases()
    return [ItemView(id=r["id"], outlet=r["outlet_name"], aliases=aliases.get(r["outlet_name"], []),
                     published=r["published_at"], text=f"{r['title']}\n{r['full_text'] or r['excerpt']}",
                     embedding=np.array(json.loads(r["embedding"]), dtype=np.float32),
                     url_hash=bytes(r["url_hash"]), content_hash=bytes(r["content_hash"])) for r in rows]


def assign_groups(item_ids: list[int]) -> int:
    """Group the items in the day window around `item_ids` and store the groups; returns the number of
    stored groups changed."""
    cfg = config.thresholds()["dedup"]
    pad = dt.timedelta(days=cfg["day_window"])
    with pool().connection() as c:
        span = c.execute("SELECT min(published_at) AS lo, max(published_at) AS hi FROM items WHERE id = ANY(%s)",
                         (item_ids,)).fetchone()
        rows = c.execute(
            "SELECT i.id, i.title, i.excerpt, i.full_text, i.published_at, i.embedding::text AS embedding, i.url_hash, "
            "i.content_hash, i.origin_group_id, s.outlet_name FROM items i JOIN sources s ON s.id=i.source_id "
            "WHERE i.published_at BETWEEN %s AND %s ORDER BY i.id",
            (span["lo"] - pad, span["hi"] + pad)).fetchall()
    stored = {r["id"]: r["origin_group_id"] for r in rows}
    result = group(_views(rows), cfg)
    components: dict[int, list[int]] = {}
    for iid, (root, _) in result.items():
        components.setdefault(root, []).append(iid)
    changed = 0
    with pool().connection() as c, c.transaction():
        for root, members in components.items():
            method = result[root][1]
            existing = sorted({stored[m] for m in members if stored[m] is not None})
            if existing:
                target = existing[0]
                if len(members) > 1:
                    c.execute("UPDATE origin_groups SET method=%s WHERE id=%s AND method='single'", (method, target))
                if len(existing) > 1:            # new evidence joins two stored groups: merge into the older
                    c.execute("UPDATE items SET origin_group_id=%s WHERE origin_group_id = ANY(%s)", (target, existing[1:]))
            else:
                target = c.execute("INSERT INTO origin_groups (method) VALUES (%s) RETURNING id",
                                   (method if len(members) > 1 else "single",)).fetchone()["id"]
            todo = [m for m in members if stored[m] != target]
            if todo:
                c.execute("UPDATE items SET origin_group_id=%s WHERE id = ANY(%s)", (target, todo))
                changed += 1
    return changed
