"""Stage 2, dedup: make sure every candidate has an origin group (ingest assigns them; this re-runs the
grouping for any item still without one), then keep at most 3 items per origin group and the best 40
(architecture D.2 step 4, FR5)."""
from __future__ import annotations

from ... import config
from ...processing import ingest, retrieve
from ...store.db import pool
from ..kit import Ctx, StageResult


def run(ctx: Ctx) -> StageResult:
    cands = ctx.output("collect").get("candidates", [])
    ids = [c["id"] for c in cands]
    if ids:
        with pool().connection() as c:
            missing = [r["id"] for r in c.execute("SELECT id FROM items WHERE id = ANY(%s) AND origin_group_id IS NULL",
                                                  (ids,))]
        if missing:
            ingest.assign_groups(missing)
        with pool().connection() as c:
            groups = {r["id"]: r["origin_group_id"] for r in
                      c.execute("SELECT id, origin_group_id FROM items WHERE id = ANY(%s)", (ids,))}
    else:
        groups = {}
    cfg = config.thresholds()["retrieval"]
    rows = [{"id": c["id"], "origin_group_id": groups.get(c["id"]), "score": c["score"]} for c in cands]
    kept = retrieve.cap_per_group(rows, cfg["max_per_origin_group"], cfg["top_items"])
    n_groups = len({r["origin_group_id"] for r in rows})
    ctx.stats.update({"origin_groups": n_groups, "items_kept": len(kept),
                      "duplicates_grouped": len(rows) - n_groups})
    return StageResult({"items": [r["id"] for r in kept], "groups": n_groups})
