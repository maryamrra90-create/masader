"""Stage 7, validate: the output validator (architecture D.2 step 11, FR9, NFR quality).

1. the splitter turns the draft into numbered checkable assertions (what_to_watch is not checked);
2. rule checks (masader.rules.check_assertion): citation present, numbers / dates / speaker in the cited
   evidence, wording that matches the label (attribution, both versions, no forecast in findings);
3. the support check, on a model other than the writer's (NoIndependentModel -> human review);
4. failed assertions get one rewrite by the writer and are checked again; still failing -> removed;
5. more than 20% removed -> the report goes to review instead of delivery.
"""
from __future__ import annotations

from ... import config
from ...gateway.client import base_model
from ...rules import Assertion, check_assertion
from ..kit import Ctx, StageResult, run_calls
from .brief import labelled_claims, rule_inputs
from .write import WRITER_FIELDS

CHECKED = ("key_findings", "by_the_numbers", "disputed", "actors", "analysis")


def _evidence(cites: list[int], by_id: dict[int, dict]) -> list[str]:
    out = []
    for i in cites:
        if i in by_id:
            out.append(by_id[i]["text"])
            out += by_id[i]["passages"]
            if by_id[i].get("outlet"):
                out.append(f"Source: {by_id[i]['outlet']}")
    return out


def _check(ctx: Ctx, assertions: list[dict], by_id, cmap, lmap, exclude) -> dict[int, list[str]]:
    """Rule check, then support check for those that pass the rules. Returns {n: fail reasons}."""
    fails = {}
    for a in assertions:
        cites = [int(x) for x in a["cites"] if isinstance(x, int)]
        fails[a["n"]] = check_assertion(Assertion(a["n"], a["section"], a["text"], cites, a.get("speaker")), cmap, lmap)
    to_support = [a for a in assertions if not fails[a["n"]]]
    if to_support:
        payload = {"assertions": [{"n": a["n"], "text": a["text"], "cites": a["cites"],
                                   "evidence": _evidence(a["cites"], by_id)} for a in to_support]}
        res = run_calls(ctx, "support", [payload], exclude=exclude)[0]
        answered = {r["n"]: r for r in res.output["results"]}
        for a in to_support:
            r = answered.get(a["n"])
            if r is None:
                fails[a["n"]] = ["support_check_missing"]
            elif r["result"] == "fail":
                fails[a["n"]] = [f"unsupported: {r['reason']}"]
    return fails


def run(ctx: Ctx) -> StageResult:
    draft = ctx.output("write")
    sections = draft.get("sections") or {}
    claims = labelled_claims(ctx.report_id, ctx.output("label"))
    by_id = {c["id"]: c for c in claims}
    cmap, lmap = rule_inputs(claims)
    usable_ids = {c["id"] for c in claims if c["label"] != "insufficient"}
    watch = [s for s in sections.get("what_to_watch", []) if s["cites"] and set(s["cites"]) <= usable_ids]
    if not any(sections.get(k) for k in CHECKED):
        return StageResult({"sections": {"what_to_watch": watch}, "assertions": [], "removed_share": 0.0})
    exclude = (base_model(draft.get("writer_model")),) if draft.get("writer_model") else ()
    assertions = run_calls(ctx, "splitter", [{"sections": {k: sections.get(k, []) for k in CHECKED}}])[0].output["assertions"]
    fails = _check(ctx, assertions, by_id, cmap, lmap, exclude)
    final = {a["n"]: dict(a, result="pass", fail_reason=None) for a in assertions if not fails[a["n"]]}
    failing = [a for a in assertions if fails[a["n"]]]
    if failing:
        req = {k: ctx.request[k] for k in ("topic", "questions", "from", "to") if k in ctx.request}
        payload = {"request": req, "claims": [{k: c[k] for k in WRITER_FIELDS} for c in claims if c["label"] != "insufficient"],
                   "rewrite": [{"n": a["n"], "section": a["section"], "text": a["text"], "cites": a["cites"],
                                "reasons": fails[a["n"]]} for a in failing]}
        rewritten = run_calls(ctx, "writer", [payload])[0].output["sections"]
        candidates = []
        for section in CHECKED:                        # match rewritten sentences to the failing ones, in order
            olds = [a for a in failing if a["section"] == section]
            for old, new in zip(olds, rewritten.get(section, []), strict=False):
                candidates.append({"n": old["n"], "section": section, "text": new["text"], "cites": new["cites"],
                                   "speaker": old.get("speaker"), "old": old})
        refails = _check(ctx, candidates, by_id, cmap, lmap, exclude) if candidates else {}
        redone = {c["n"]: c for c in candidates}
        for a in failing:
            c = redone.get(a["n"])
            if c is not None and not refails.get(a["n"]):
                final[a["n"]] = {"n": a["n"], "section": a["section"], "text": c["text"], "cites": c["cites"],
                                 "speaker": c.get("speaker"), "result": "rewritten", "fail_reason": "; ".join(fails[a["n"]])}
            else:
                why = fails[a["n"]] + (refails.get(a["n"], []) if c is not None else ["no rewrite returned"])
                final[a["n"]] = dict(a, result="removed", fail_reason="; ".join(why)[:500])
    removed = sum(1 for a in final.values() if a["result"] == "removed")
    share = removed / len(final) if final else 0.0
    limit = config.thresholds()["validation"]["review_if_removed_share_over"]
    out_sections: dict[str, list] = {k: [] for k in CHECKED}
    for n in sorted(final):
        a = final[n]
        if a["result"] != "removed":
            out_sections.setdefault(a["section"], []).append({"text": a["text"], "cites": a["cites"]})
    out_sections["what_to_watch"] = watch
    ctx.stats.update({"assertions": len(final), "assertions_rewritten": sum(1 for a in final.values() if a["result"] == "rewritten"),
                      "assertions_removed": removed})
    review = f"{removed} of {len(final)} assertions ({share:.0%}) failed validation, more than {limit:.0%}" if share > limit else None

    def write(c):
        c.execute("DELETE FROM report_assertions WHERE report_id=%s", (ctx.report_id,))
        for a in final.values():
            c.execute("INSERT INTO report_assertions (report_id, assertion_no, section, text, cited_claim_ids, result, fail_reason) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s)", (ctx.report_id, a["n"], a["section"], a["text"],
                                                       [int(x) for x in a["cites"]], a["result"], a["fail_reason"]))
    return StageResult({"sections": out_sections, "removed_share": round(share, 4), "review_reason": review}, write)
