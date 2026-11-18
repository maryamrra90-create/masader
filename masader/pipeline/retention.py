"""Daily retention task (NFR9, architecture I10): raw full text and raw source objects are purged after
each source's fulltext_days (default 30); the excerpt, title and embedding stay for 12 months so stored
reports keep their evidence passages. AI call traces (ai_calls rows) are deleted after 90 days; the
per-purpose budget totals live in the budgets table and are not affected."""
from __future__ import annotations

import logging

from ..store.db import pool
from ..store.objectstore import get_store

log = logging.getLogger(__name__)
TRACE_DAYS = 90
EXCERPT_MONTHS = 12


def run_retention() -> dict:
    store = get_store()
    with pool().connection() as c:
        rows = c.execute("""UPDATE items i SET full_text = NULL, text_purged_at = now(), raw_object_key = NULL
                            FROM sources s, (SELECT id, raw_object_key AS old_key FROM items) o
                            WHERE s.id = i.source_id AND o.id = i.id AND i.text_purged_at IS NULL
                              AND i.fetched_at < now() - make_interval(days => s.fulltext_days)
                            RETURNING i.id, o.old_key""").fetchall()
        traces = c.execute("DELETE FROM ai_calls WHERE created_at < now() - make_interval(days => %s) AND outcome <> 'started'",
                           (TRACE_DAYS,)).rowcount
    removed = 0
    for r in rows:
        if r["old_key"]:
            try:
                store.delete(r["old_key"])
                removed += 1
            except Exception:  # noqa: BLE001 - a missing object must not stop the purge
                log.warning("raw object %s could not be deleted", r["old_key"])
    out = {"items_text_purged": len(rows), "raw_objects_deleted": removed, "ai_call_traces_deleted": traces}
    log.info("retention: %s", out)
    return out
