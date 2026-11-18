"""Hybrid retrieval inside the request's date window (architecture D.2 step 3, ADR-04).

Score = semantic_weight x exact cosine (pgvector `<=>`, no ANN index) + (1 - semantic_weight) x full-text
rank (normalised to the best rank in the window). An item needs a full-text match or a cosine of at least
MIN_COSINE to be a candidate, so unrelated items in the window are not collected.
"""
from __future__ import annotations

import datetime as dt
import re

from .. import config
from ..store.db import pool
from ..simulated.heuristics import STOP
from .embed import get_embedder, to_pgvector

MIN_COSINE = 0.30     # measured on the corpus with the hashing embedder: unrelated items reach about 0.2


def _tsquery(terms: list[str]) -> str:
    """OR of prefix terms, stop words removed (otherwise 'and:*' alone would match almost every item)."""
    words = []
    for t in terms:
        for w in re.findall(r"[\w؀-ۿ]+", t.lower()):
            if len(w) > 2 and w not in STOP and not w.isdigit() and w not in words:
                words.append(w)
    return " | ".join(f"{w}:*" for w in words[:24]) or "nothing_to_match"


def window(date_from: dt.date, date_to: dt.date) -> tuple[dt.datetime, dt.datetime]:
    lo = dt.datetime.combine(date_from, dt.time.min, tzinfo=dt.timezone.utc) - dt.timedelta(hours=4)
    hi = dt.datetime.combine(date_to, dt.time.max, tzinfo=dt.timezone.utc)
    return lo, hi


def hit_count(terms: list[str], date_from: dt.date, date_to: dt.date) -> int:
    """Full-text hits in the window (planner rules 'too broad' / 'too little evidence')."""
    lo, hi = window(date_from, date_to)
    with pool().connection() as c:
        return c.execute("SELECT count(*) AS n FROM items i JOIN sources s ON s.id=i.source_id WHERE s.active "
                         "AND i.published_at BETWEEN %s AND %s AND i.fts @@ to_tsquery('simple', %s)",
                         (lo, hi, _tsquery(terms))).fetchone()["n"]


def candidates(queries_en: list[str], queries_ar: list[str], date_from: dt.date, date_to: dt.date,
               exclude_sources: list[str] | None = None, limit: int = 80) -> list[dict]:
    """Candidate items ranked by the hybrid score, best first."""
    w = config.thresholds()["retrieval"]["semantic_weight"]
    qvec = get_embedder().embed([" ".join(queries_en)])[0]
    lo, hi = window(date_from, date_to)
    with pool().connection() as c:
        rows = c.execute(
            "WITH q AS (SELECT to_tsquery('simple', %s) AS tq), "
            "scored AS (SELECT i.id, i.origin_group_id, i.published_at, s.code, "
            "  1 - (i.embedding <=> %s::vector) AS cos, ts_rank(i.fts, q.tq) AS rank "
            "  FROM items i JOIN sources s ON s.id=i.source_id, q "
            "  WHERE s.active AND i.published_at BETWEEN %s AND %s AND NOT (s.code = ANY(%s))) "
            "SELECT *, %s * cos + (1 - %s) * coalesce(rank / nullif(max(rank) OVER (), 0), 0) AS score FROM scored "
            "WHERE rank > 0 OR cos >= %s ORDER BY score DESC, id LIMIT %s",
            (_tsquery(queries_en + queries_ar), to_pgvector(qvec), lo, hi, exclude_sources or [], w, w, MIN_COSINE,
             limit)).fetchall()
    return [dict(r) for r in rows]


def cap_per_group(rows: list[dict], per_group: int, top: int) -> list[dict]:
    """At most `per_group` items per origin group, then the best `top` (D.2: 40 items, 3 per group)."""
    seen: dict[int, int] = {}
    out = []
    for r in rows:
        g = r["origin_group_id"] or -r["id"]
        if seen.get(g, 0) >= per_group:
            continue
        seen[g] = seen.get(g, 0) + 1
        out.append(r)
        if len(out) == top:
            break
    return out
