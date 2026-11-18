"""Stage 8, render: the report body (ReportOut), its HTML, and both exports in the object store; the
report becomes delivered, or in_review when validation removed too much or the topic is sensitive
(architecture D.2 step 12, FR11)."""
from __future__ import annotations

import datetime as dt
import json

from ... import config
from ...render.build import TEMPLATE, TEMPLATE_VERSION, render_html
from ...rules import RULE_VERSION
from ...simulated.heuristics import best_sentence, content_words
from ...store.db import pool
from ...store.objectstore import get_store
from ..kit import Ctx, StageResult, finish
from .brief import labelled_claims
from .common import full_text, load_items


def build_body(ctx: Ctx, status: str) -> dict:
    final = ctx.output("validate")
    sections = final.get("sections") or {}
    claims = labelled_claims(ctx.report_id, ctx.output("label"))
    by_id = {c["id"]: c for c in claims}
    cited = []
    for key in ("key_findings", "by_the_numbers", "disputed", "actors", "analysis", "what_to_watch"):
        for s in sections.get(key, []):
            cited += [i for i in s["cites"] if i in by_id and i not in cited]
    fid = {cid: f"f{n + 1}" for n, cid in enumerate(cited)}
    with pool().connection() as c:
        ev = c.execute("SELECT e.claim_id, e.item_id, e.verdict::text AS verdict, e.first_hand FROM claim_evidence e "
                       "JOIN claims cl ON cl.id=e.claim_id WHERE cl.report_id=%s", (ctx.report_id,)).fetchall()
        rep = c.execute("SELECT r.*, q.payload FROM reports r JOIN requests q ON q.id=r.request_id WHERE r.id=%s",
                        (ctx.report_id,)).fetchone()
    items = load_items(sorted({e["item_id"] for e in ev}))
    findings = []
    for cid in cited:
        cl = by_id[cid]
        own = dict(zip(cl["cite_items"], [None] * len(cl["cite_items"]), strict=True))
        srcs = []
        rows = [e for e in ev if e["claim_id"] == cid and e["verdict"] in ("supports", "contradicts") and e["item_id"] in items]
        rows.sort(key=lambda e: (e["verdict"] != "supports", e["item_id"] not in own, items[e["item_id"]]["tier"]))
        for e in rows:
            it = items[e["item_id"]]
            passage = next((p for p in cl["passages"] if p.lower() in full_text(it).lower()), None) \
                or best_sentence(cl["text"], full_text(it))[1]
            srcs.append({"outlet": it["outlet_name"], "tier": it["tier"], "url": it["url"],
                         "published": it["published_at"].isoformat(), "passage": passage,
                         "first_hand": bool(e["first_hand"] or it["official"]), "origin_group": it["origin_group_id"],
                         "verdict": e["verdict"], "item_id": it["id"]})
        sup = [s for s in srcs if s["verdict"] == "supports"]
        findings.append({"id": fid[cid], "claim_id": cid, "text": cl["text"], "label": cl["label"],
                         "origin_groups": len({s["origin_group"] for s in sup}),
                         "reliability": {"best_tier": min([s["tier"] for s in sup] or ["C"]),
                                         "primary_source": any(s["first_hand"] for s in sup),
                                         "low_reliability": cl["low_reliability"], "reason": cl["reason"]},
                         "sources": srcs})
    out_sections = {k: [{"text": s["text"], "findings": [fid[i] for i in s["cites"] if i in fid]} for s in v]
                    for k, v in sections.items()}
    gaps = [f"Not established: {c['text']} ({c['reason']})" for c in claims if c["label"] == "insufficient"][:8]
    text_all = " ".join(s["text"] for v in sections.values() for s in v)
    for q in ctx.request.get("questions", []):
        qw = content_words(q) - content_words(ctx.request["topic"])
        if qw and not (qw & content_words(text_all)):
            gaps.append(f"No checked finding addresses this part of the request: \"{q}\"")
    stats = dict(rep["stats"] or {})
    versions = dict(rep["versions"] or {})
    simulated = any(str(v).startswith("sim:") for v in versions.values())
    req = ctx.request
    return {
        "report_id": ctx.report_id, "request_id": rep["request_id"], "version": rep["version"], "status": status,
        "publication": rep["publication"], "ai_generated": True,
        "review_status": "reviewed" if rep["approved_at"] else "not_reviewed",
        "title": req["topic"], "questions": req.get("questions", []), "window": {"from": req["from"], "to": req["to"]},
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "findings": findings, "sections": dict(out_sections, evidence_gaps=gaps),
        "statistics": ctx.output("collect").get("statistics", []),
        "versions": versions | {"template": f"{TEMPLATE}@{TEMPLATE_VERSION}", "config": rep["config_hash"]},
        "method": {"rule_version": RULE_VERSION, "config_hash": rep["config_hash"], "simulated": simulated,
                   "items_in_window": stats.get("items_in_window", 0), "candidates": stats.get("candidates", 0),
                   "items_kept": stats.get("items_kept", 0), "origin_groups": stats.get("origin_groups", 0),
                   "duplicates_grouped": stats.get("duplicates_grouped", 0), "claims": stats.get("claims", 0),
                   "invented_passages_dropped": stats.get("invented_passages_dropped", 0),
                   "claims_merged": stats.get("claims_merged", 0), "labels": stats.get("labels", {}),
                   "assertions": stats.get("assertions", 0), "assertions_rewritten": stats.get("assertions_rewritten", 0),
                   "assertions_removed": stats.get("assertions_removed", 0)},
        "notes": list(dict.fromkeys(rep["notes"] or [])), "change_note": rep["change_note"],
    }


def run(ctx: Ctx) -> StageResult:
    final = ctx.output("validate")
    review = final.get("review_reason")
    topic = (ctx.request["topic"] + " " + " ".join(ctx.request.get("questions", []))).lower()
    sensitive = [t for t in config.active().get("sensitive_topics", []) if t in topic]
    if sensitive:
        review = (review + "; " if review else "") + f"sensitive topic ({', '.join(sensitive)}) needs a reviewer"
    status = "in_review" if review else "delivered"
    if review:
        ctx.notes.append(f"in review: {review}")
    body = build_body(ctx, status)
    body["notes"] = list(dict.fromkeys(body["notes"] + ctx.notes))
    html = render_html(body)
    store = get_store()
    jkey = f"reports/{ctx.report_id}/v{body['version']}.json"
    hkey = f"reports/{ctx.report_id}/v{body['version']}.html"
    store.put(jkey, json.dumps(body, ensure_ascii=False, indent=1, default=str).encode(), "application/json")
    store.put(hkey, html.encode(), "text/html; charset=utf-8")
    ctx.notes.clear()                                  # already merged into body["notes"], written by finish()
    return StageResult({"status": status, "json": jkey, "html": hkey},
                       lambda c: finish(c, ctx.report_id, status, body, jkey, hkey))
