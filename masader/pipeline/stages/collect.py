"""Stage 1, collect: candidate items for the request (hybrid retrieval inside the date window) and the
reference statistics for the request's countries (architecture D.2 steps 2-3)."""
from __future__ import annotations

import datetime as dt

from ...processing import retrieve
from ...store.db import pool
from ..kit import Ctx, StageResult


def dates(req: dict) -> tuple[dt.date, dt.date]:
    return dt.date.fromisoformat(req["from"]), dt.date.fromisoformat(req["to"])


def run(ctx: Ctx) -> StageResult:
    d0, d1 = dates(ctx.request)
    q_en = ctx.plan.get("queries_en") or [ctx.request["topic"]]
    q_ar = ctx.plan.get("queries_ar") or []
    exclude = (ctx.request.get("sources") or {}).get("exclude", [])
    rows = retrieve.candidates(q_en + [ctx.request["topic"]], q_ar, d0, d1, exclude_sources=exclude)
    with pool().connection() as c:
        n_window = c.execute("SELECT count(*) AS n FROM items WHERE published_at BETWEEN %s AND %s",
                             retrieve.window(d0, d1)).fetchone()["n"]
        stats = c.execute(
            "SELECT DISTINCT ON (st.indicator, st.country, st.year) st.indicator, st.country, st.year, st.value::float AS value, "
            "st.last_updated, s.outlet_name, s.tier::text AS tier FROM statistics st JOIN sources s ON s.id = st.source_id "
            "WHERE s.active AND st.country = ANY(%s) ORDER BY st.indicator, st.country, st.year DESC, st.retrieved_at DESC",
            (ctx.request.get("geography") or [],)).fetchall()
    latest: dict[tuple, dict] = {}
    for s in stats:                                    # newest year per indicator and country
        k = (s["indicator"], s["country"])
        if k not in latest or s["year"] > latest[k]["year"]:
            latest[k] = s
    if not rows:
        ctx.notes.append("collect: no item in the date window matched the request")
    ctx.stats.update({"items_in_window": n_window, "candidates": len(rows)})
    return StageResult({"candidates": [{"id": r["id"], "score": round(float(r["score"]), 4)} for r in rows],
                        "statistics": [dict(v, last_updated=str(v["last_updated"]) if v["last_updated"] else None)
                                       for v in latest.values()]})
