"""Stage 3, extract: claims from the kept items, 5 items per call (architecture D.2 steps 5-6, FR6).

After the calls: a claim whose passage is not found verbatim in its item is dropped as an invented
citation; claims attributed to companies are dropped if the requester chose official sources and news
only; near-identical claims (cosine >= 0.9) from different items are merged and keep every passage."""
from __future__ import annotations

import json

import numpy as np

from ... import config
from ...processing.embed import get_embedder
from ...processing.text import normalise, passage_in
from ...simulated.heuristics import COMPANY
from ..kit import Ctx, StageResult, run_calls
from .common import chunks, full_text, load_items, select_passages, source_block


def run(ctx: Ctx) -> StageResult:
    cfg = config.thresholds()
    ids = ctx.output("dedup").get("items", [])
    items = load_items(ids)
    focus = " ".join([ctx.request["topic"]] + ctx.request.get("questions", []))
    req = {k: ctx.request[k] for k in ("topic", "questions", "from", "to") if k in ctx.request}
    payloads = []
    for batch in chunks([i for i in ids if i in items], cfg["extraction"]["items_per_call"]):
        blocks = [source_block(items[i], select_passages(items[i], focus, cfg["retrieval"]["passages_per_item"],
                                                         cfg["retrieval"]["paragraph_chars"])) for i in batch]
        payloads.append({"request": req, "items": blocks})
    results = run_calls(ctx, "extractor", payloads)
    raw, invented, foreign, company = [], 0, 0, 0
    official_only = ctx.answers.get("q1") == "official_and_news_only"
    for payload, res in zip(payloads, results, strict=True):
        allowed = {b["id"] for b in payload["items"]}
        for cl in res.output["claims"][: cfg["extraction"]["claims_per_call_max"]]:
            if cl["item_id"] not in allowed:
                foreign += 1
                continue
            if not passage_in(cl["passage"], full_text(items[cl["item_id"]])):
                invented += 1
                continue
            if official_only and cl.get("speaker") and COMPANY.search(cl["speaker"]):
                company += 1
                continue
            raw.append(cl)
    merged = merge(raw, cfg["extraction"]["merge_cosine"])
    version = f"{results[0].prompt_version}@{results[0].model}" if results else "none"
    ctx.stats.update({"claims_extracted": len(raw) + invented + foreign + company, "invented_passages_dropped": invented,
                      "claims_for_unknown_items_dropped": foreign, "company_statements_dropped": company,
                      "claims_merged": len(raw) - len(merged), "claims": len(merged)})
    if invented:
        ctx.notes.append(f"extract: {invented} claim(s) dropped because their passage was not in the source")

    def write(c):
        c.execute("DELETE FROM claims WHERE report_id=%s", (ctx.report_id,))        # a re-run replaces the outputs
        for cl in merged:
            cid = c.execute("INSERT INTO claims (report_id, text, claim_type, speaker, numbers, dates, entities, "
                            "extractor_version, item_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                            (ctx.report_id, cl["text"], cl["type"], cl.get("speaker"), json.dumps(cl.get("numbers", [])),
                             json.dumps(cl.get("dates", [])), json.dumps(cl.get("entities", [])), version,
                             cl["item_id"])).fetchone()["id"]
            for iid, passage in cl["passages"]:
                text = full_text(items[iid])
                start = normalise(text).find(normalise(passage))
                c.execute("INSERT INTO claim_passages (claim_id, item_id, passage, char_start, char_end, passage_found) "
                          "VALUES (%s,%s,%s,%s,%s,true) ON CONFLICT DO NOTHING",
                          (cid, iid, passage, start if start >= 0 else None,
                           start + len(passage) if start >= 0 else None))
    return StageResult({"claims": len(merged)}, write)


def merge(claims: list[dict], threshold: float) -> list[dict]:
    """Greedy merge of near-identical claims with the same figures (two sentences that differ only in a
    number are two different claims); the first one keeps its text, every passage is kept."""
    if not claims:
        return []
    vecs = get_embedder().embed([c["text"] for c in claims])
    kept: list[dict] = []
    kept_vecs: list[np.ndarray] = []
    for cl, v in zip(claims, vecs, strict=True):
        if kept_vecs:
            sims = np.array(kept_vecs) @ v
            j = int(np.argmax(sims))
            if sims[j] >= threshold and set(kept[j].get("numbers", [])) == set(cl.get("numbers", [])):
                if all(iid != cl["item_id"] for iid, _ in kept[j]["passages"]):
                    kept[j]["passages"].append((cl["item_id"], cl["passage"]))
                continue
        kept.append(dict(cl, passages=[(cl["item_id"], cl["passage"])]))
        kept_vecs.append(v)
    return kept
