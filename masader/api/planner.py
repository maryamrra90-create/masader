"""Request planner (proposal A1, architecture D.2 step 1): deterministic rules first, then one model call
(planner role, 8 s budget) for search terms and any further question. At most 3 questions, each with a
default. If the model call fails, the rule-based plan is used and the failure is noted."""
from __future__ import annotations

import datetime as dt
import re

from .. import config
from ..gateway.client import one_off
from ..gateway.errors import GatewayError
from ..processing import retrieve
from ..settings import settings

UNSUPPORTED = re.compile(r"\b(predict|forecast (?:the )?(?:price|outcome)|price target|investment advice|stock tips|"
                         r"buy or sell|who will win|guarantee)\b", re.I)


def ambiguous(req: dict) -> list[dict]:
    text = " ".join([req["topic"]] + req.get("questions", []))
    ents = " ".join(req.get("entities", []))
    out = []
    for name, cands in config.gazetteer().items():
        if re.search(rf"\b{re.escape(name)}\b", text) and not any(c.split(" (")[0] in ents or c in ents for c in cands):
            out.append({"name": name, "candidates": cands})
    return out


def rule_questions(req: dict, hits: int) -> list[dict]:
    cfg = config.thresholds()["planner"]
    qs = []
    for a in ambiguous(req):
        qs.append({"id": f"entity_{a['name'].lower()}", "text": f"Which {a['name']} do you mean?",
                   "default": a["candidates"][0], "options": a["candidates"]})
    if hits > cfg["too_broad_hits"]:
        qs.append({"id": "narrow", "text": f"The request matches {hits:,} items. Narrow it to one country, actor or aspect "
                   "(type it), or proceed with the most relevant items?", "default": "proceed_top_items",
                   "options": ["proceed_top_items"]})
    if hits < cfg["too_little_evidence_hits"]:
        qs.append({"id": "widen", "text": f"Only {hits} item(s) match in this date range. Widen the range to 30 days?",
                   "default": "widen_30_days", "options": ["widen_30_days", "keep_range"]})
    text = " ".join([req["topic"]] + req.get("questions", []))
    if UNSUPPORTED.search(text):
        qs.append({"id": "scope", "text": "Masader reports what sources say; it does not make predictions or give advice. "
                   "Continue with a brief of reported facts and stated expectations?",
                   "default": "reported_facts_only", "options": ["reported_facts_only"]})
    return qs


def plan(req: dict, purpose: str = "live") -> tuple[dict, list[dict]]:
    """Returns (plan, questions). plan holds the search terms and how it was made."""
    d0, d1 = dt.date.fromisoformat(req["from"]), dt.date.fromisoformat(req["to"])
    terms = [req["topic"]] + req.get("questions", []) + req.get("entities", [])
    hits = retrieve.hit_count(terms, d0, d1)
    questions = rule_questions(req, hits)
    model_req = {k: req[k] for k in ("topic", "questions", "from", "to", "geography", "entities", "sources") if k in req}
    p: dict = {"hits": hits, "purpose": purpose, "queries_en": [req["topic"]], "queries_ar": [], "planner": "rules"}
    try:
        res = one_off("planner", {"request": model_req}, timeout_s=settings.planner_timeout_s, purpose=purpose)
        out = res.output
        p.update(queries_en=out["queries_en"] or [req["topic"]], queries_ar=out["queries_ar"], planner=res.model)
        have = {q["id"] for q in questions}
        for q in out["questions"]:
            if q["id"] not in have and q.get("options"):
                questions.append(q)
        for a in out["ambiguous_entities"]:
            qid = f"entity_{a['name'].lower()}"
            if qid not in have and a.get("candidates"):
                questions.append({"id": qid, "text": f"Which {a['name']} do you mean?", "default": a["candidates"][0],
                                  "options": a["candidates"]})
    except GatewayError as e:
        p["planner_note"] = f"planner model unavailable ({type(e).__name__}); rule-based plan used"
    return p, questions[:3]


def apply_answers(req: dict, plan_: dict, questions: list[dict], answers: dict) -> tuple[dict, dict]:
    """Defaults fill unanswered questions; answers change the request where they mean something."""
    req = dict(req)
    full = {q["id"]: answers.get(q["id"], q["default"]) for q in questions}
    if full.get("widen") == "widen_30_days":
        d1 = dt.date.fromisoformat(req["to"])
        req["from"] = (d1 - dt.timedelta(days=30)).isoformat()
    n = full.get("narrow")
    if n and n != "proceed_top_items":
        req["entities"] = (req.get("entities") or []) + [n[:100]]
        plan_ = dict(plan_, queries_en=[f"{req['topic']} {n}"] + plan_.get("queries_en", []))
    for qid, v in full.items():
        if qid.startswith("entity_"):
            req["entities"] = (req.get("entities") or []) + [v]
    return req, full
