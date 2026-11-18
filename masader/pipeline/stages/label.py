"""Stage 5, label: the evidence rules (rules-1.1.1, masader.rules) turn verdicts, origin groups and tiers
into a label and a reliability reason for every claim. No AI is involved (architecture D.2 step 8, FR8)."""
from __future__ import annotations

import json
import re

from ...rules import RULE_VERSION, Claim, Evidence, label_claim
from ...simulated.heuristics import best_sentence
from ...store.db import pool
from ..kit import Ctx, StageResult
from .common import full_text


def figure(claim_text: str, item_text: str) -> str | None:
    """The first figure (not a year) in the item sentence closest to the claim: what this passage says
    the number is, so the rules can spot superseded and secondhand figures."""
    _, sent = best_sentence(claim_text, item_text)
    for n in re.findall(r"\d+(?:[.,]\d+)?", sent):
        if not re.fullmatch(r"(19|20)\d\d", n):
            return n
    return None


def evidence_for(report_id: str) -> tuple[list[dict], dict[int, list[dict]], dict[int, list[str]]]:
    with pool().connection() as c:
        claims = c.execute("SELECT id, text, claim_type::text AS type, speaker FROM claims WHERE report_id=%s ORDER BY id",
                           (report_id,)).fetchall()
        ev = c.execute(
            "SELECT e.claim_id, e.item_id, e.verdict::text AS verdict, e.first_hand, i.published_at, i.title, "
            "coalesce(i.full_text, i.excerpt) AS text, i.origin_group_id, s.outlet_name, s.tier::text AS tier, s.official "
            "FROM claim_evidence e JOIN claims cl ON cl.id=e.claim_id JOIN items i ON i.id=e.item_id "
            "JOIN sources s ON s.id=i.source_id WHERE cl.report_id=%s", (report_id,)).fetchall()
        passages = c.execute("SELECT p.claim_id, p.passage FROM claim_passages p JOIN claims cl ON cl.id=p.claim_id "
                             "WHERE cl.report_id=%s", (report_id,)).fetchall()
    by_claim: dict[int, list[dict]] = {}
    for e in ev:
        by_claim.setdefault(e["claim_id"], []).append(dict(e))
    pas: dict[int, list[str]] = {}
    for p in passages:
        pas.setdefault(p["claim_id"], []).append(p["passage"])
    return [dict(c) for c in claims], by_claim, pas


def run(ctx: Ctx) -> StageResult:
    claims, by_claim, pas = evidence_for(ctx.report_id)
    labels, extra = {}, {}
    for cl in claims:
        evidence = [Evidence(item_id=e["item_id"], origin_group=e["origin_group_id"] or -e["item_id"], tier=e["tier"],
                             verdict=e["verdict"], first_hand=e["first_hand"] or e["official"], published=e["published_at"],
                             publisher=e["outlet_name"], official=e["official"],
                             value=figure(cl["text"], full_text(e)) if e["verdict"] != "not_addressed" else None)
                    for e in by_claim.get(cl["id"], [])]
        lab = label_claim(Claim(cl["id"], cl["text"], cl["type"], cl["speaker"], passages=pas.get(cl["id"], [])), evidence)
        labels[cl["id"]] = lab
        extra[str(cl["id"])] = {"cite_items": lab.cite_items, "attribution_required": lab.attribution_required,
                                "replaced": lab.replaced,
                                "contradicting": [e.item_id for e in evidence if e.verdict == "contradicts"]}
    counts = {k: sum(1 for v in labels.values() if v.label == k)
              for k in ("corroborated", "single_source", "disputed", "insufficient")}
    counts["low_reliability"] = sum(1 for v in labels.values() if v.low_reliability)
    ctx.stats.update({"labels": counts})

    def write(c):
        c.execute("DELETE FROM claim_labels WHERE claim_id IN (SELECT id FROM claims WHERE report_id=%s)", (ctx.report_id,))
        for cid, lab in labels.items():
            c.execute("INSERT INTO claim_labels (claim_id, rule_version, label, low_reliability, reliability_reason) "
                      "VALUES (%s,%s,%s,%s,%s)", (cid, RULE_VERSION, lab.label, lab.low_reliability, lab.reason))
    return StageResult({"labels": extra, "rule_version": RULE_VERSION}, write)


__all__ = ["run", "evidence_for", "figure", "json"]
