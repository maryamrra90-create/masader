"""Loading labelled claims for the writer and the validator (shared by stages write and validate)."""
from __future__ import annotations

from ...rules import Claim, Label
from ...store.db import pool


def labelled_claims(report_id: str, label_out: dict) -> list[dict]:
    """Claims with their label, reliability, the outlet to attribute them to, and the ids of the claims
    that give the other version of a disputed one."""
    extra = label_out.get("labels", {})
    with pool().connection() as c:
        rows = c.execute(
            "SELECT cl.id, cl.text, cl.claim_type::text AS type, cl.speaker, cl.numbers, cl.item_id, l.label::text AS label, "
            "l.low_reliability, l.reliability_reason FROM claims cl JOIN claim_labels l ON l.claim_id=cl.id "
            "WHERE cl.report_id=%s ORDER BY cl.id", (report_id,)).fetchall()
        outlets = {r["id"]: r["outlet_name"] for r in c.execute(
            "SELECT i.id, s.outlet_name FROM items i JOIN sources s ON s.id=i.source_id WHERE i.id IN "
            "(SELECT p.item_id FROM claim_passages p JOIN claims cl ON cl.id=p.claim_id WHERE cl.report_id=%s "
            " UNION SELECT e.item_id FROM claim_evidence e JOIN claims cl ON cl.id=e.claim_id WHERE cl.report_id=%s)",
            (report_id, report_id))}
        own = {}
        for r in c.execute("SELECT p.claim_id, p.item_id, p.passage FROM claim_passages p JOIN claims cl ON cl.id=p.claim_id "
                           "WHERE cl.report_id=%s ORDER BY p.item_id", (report_id,)):
            own.setdefault(r["claim_id"], []).append((r["item_id"], r["passage"]))
    out = []
    for r in rows:
        x = extra.get(str(r["id"]), {})
        cite = x.get("cite_items") or [r["item_id"]]
        versions = [o["id"] for o in rows if o["id"] != r["id"] and any(i in x.get("contradicting", []) for i, _ in own.get(o["id"], []))]
        out.append({"id": r["id"], "text": r["text"], "type": r["type"], "speaker": r["speaker"], "numbers": r["numbers"],
                    "label": r["label"], "low_reliability": r["low_reliability"], "reason": r["reliability_reason"],
                    "attribution_required": bool(x.get("attribution_required")), "outlet": outlets.get(cite[0]),
                    "versions": versions, "passages": [p for _, p in own.get(r["id"], [])],
                    "cite_items": cite})
    return out


def rule_inputs(claims: list[dict]) -> tuple[dict[int, Claim], dict[int, Label]]:
    cmap = {c["id"]: Claim(c["id"], c["text"], c["type"], c["speaker"], passages=c["passages"]) for c in claims}
    lmap = {c["id"]: Label(c["label"], c["low_reliability"], c["attribution_required"], c["cite_items"], c["reason"])
            for c in claims}
    return cmap, lmap
