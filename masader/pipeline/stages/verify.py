"""Stage 4, verify: each claim against its own items and up to 4 other candidate items (most similar by
embedding), 5 claims per call; one verdict per (claim, item) pair (architecture D.2 step 7, FR7)."""
from __future__ import annotations

import json

import numpy as np

from ... import config
from ...processing.embed import get_embedder
from ...store.db import pool
from ..kit import Ctx, StageResult, run_calls
from .common import chunks, load_items, select_passages, source_block


def run(ctx: Ctx) -> StageResult:
    cfg = config.thresholds()
    pool_ids = ctx.output("dedup").get("items", [])
    items = load_items(pool_ids)
    with pool().connection() as c:
        claims = c.execute("SELECT id, text, claim_type::text AS type, speaker FROM claims WHERE report_id=%s ORDER BY id",
                           (ctx.report_id,)).fetchall()
        own: dict[int, set[int]] = {}
        for r in c.execute("SELECT p.claim_id, p.item_id FROM claim_passages p JOIN claims cl ON cl.id=p.claim_id "
                           "WHERE cl.report_id=%s", (ctx.report_id,)):
            own.setdefault(r["claim_id"], set()).add(r["item_id"])
    ids = [i for i in pool_ids if i in items]
    if ids and claims:
        mat = np.stack([np.array(json.loads(items[i]["embedding"]), dtype=np.float32) for i in ids])
        cvecs = get_embedder().embed([c["text"] for c in claims])
    pairs_by_claim: dict[int, list[int]] = {}
    for n, cl in enumerate(claims):
        mine = sorted(i for i in own.get(cl["id"], set()) if i in items)
        others = []
        if ids:
            sims = mat @ cvecs[n]
            for j in np.argsort(-sims):
                iid = ids[int(j)]
                if iid not in mine:
                    others.append(iid)
                if len(others) == cfg["verification"]["candidates_per_claim"]:
                    break
        pairs_by_claim[cl["id"]] = mine + others
    payloads = []
    for batch in chunks(claims, cfg["verification"]["claims_per_call"]):
        pairs = [[cl["id"], iid] for cl in batch for iid in pairs_by_claim[cl["id"]]]
        need = sorted({iid for _, iid in pairs})
        focus = " ".join(cl["text"] for cl in batch)
        blocks = [source_block(items[i], select_passages(items[i], focus, 3, cfg["retrieval"]["paragraph_chars"]))
                  for i in need]
        payloads.append({"claims": [dict(cl) for cl in batch], "items": blocks, "pairs": pairs})
    results = run_calls(ctx, "verifier", payloads)
    rows, unexpected = {}, 0
    for payload, res in zip(payloads, results):
        asked = {tuple(p) for p in payload["pairs"]}
        for r in res.output["results"]:
            key = (r["claim_id"], r["item_id"])
            if key not in asked:
                unexpected += 1
                continue
            rows[key] = r
    version = f"{results[0].prompt_version}@{results[0].model}" if results else "none"
    counts = {v: sum(1 for r in rows.values() if r["verdict"] == v) for v in ("supports", "contradicts", "not_addressed")}
    ctx.stats.update({"verdicts": counts, "verdicts_for_unasked_pairs_dropped": unexpected,
                      "pairs_without_verdict": sum(len(v) for v in pairs_by_claim.values()) - len(rows)})

    def write(c):
        c.execute("DELETE FROM claim_evidence WHERE claim_id IN (SELECT id FROM claims WHERE report_id=%s)", (ctx.report_id,))
        for r in rows.values():
            c.execute("INSERT INTO claim_evidence (claim_id, item_id, verdict, reason, first_hand, verifier_version) "
                      "VALUES (%s,%s,%s,%s,%s,%s)", (r["claim_id"], r["item_id"], r["verdict"], r["reason"][:500],
                                                     r["first_hand"], version))
    return StageResult({"pairs": len(rows)}, write)
