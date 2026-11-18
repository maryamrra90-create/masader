"""Seeded-error detection of the output validator (proposal target: >= 95% of seeded errors caught, with
few correct sentences blocked).

The validator = the rule checks (masader.rules.check_assertion) + the support check. In this build the
support check is the SIMULATED one (masader.simulated.heuristics.support, a lexical check), because no
API key was available, so these numbers describe the rules plus that heuristic, NOT Claude Haiku.

Two sets:
  A. the 20 hand-written E3 seeded assertions and 10 correct ones (architecture experiment E3). The
     heuristic was written knowing them, so set A is optimistic.
  B. generated from a pipeline report on the synthetic corpus: every assertion that passed validation
     is mutated in 9 ways (number, date, speaker, direction, negation, added unsupported clause, missing
     attribution, forecast wording in findings, dropped citation); the unmodified assertions measure
     false blocks. The mutation code was written before the run and is not tuned on its result.
Run: python eval/seeded_errors.py   (needs PostgreSQL and Redis; uses the DATABASE_URL database)
"""
from __future__ import annotations

import random
import re

from common import HORMUZ, client_and_key, prepare, run_report, save  # noqa: I001

from eval import cases_e3 as E3
from masader.pipeline.stages.brief import labelled_claims, rule_inputs
from masader.rules import Assertion, check_assertion
from masader.simulated.heuristics import support
from masader.store.db import pool

MONTHS = "January February March April May June July August September October November December".split()


def validate(a: Assertion, claims, labels, evidence_of) -> tuple[bool, str]:
    fails = check_assertion(a, claims, labels)
    if fails:
        return True, "rules: " + ",".join(fails)
    res = support({"assertions": [{"n": a.n, "text": a.text, "cites": a.cited, "evidence": evidence_of(a.cited)}]})
    r = res["results"][0]
    return r["result"] == "fail", f"support: {r['reason']}"


def set_a() -> dict:
    def ev(cites):
        return [t for i in cites if i in E3.CLAIMS for t in [E3.CLAIMS[i].text, *E3.CLAIMS[i].passages]]
    seeded = [{"type": t, "n": a.n, "caught": c, "by": why}
              for t, a in E3.SEEDED for c, why in [validate(a, E3.CLAIMS, E3.LABELS, ev)]]
    good = [{"n": a.n, "blocked": c, "by": why} for a in E3.GOOD for c, why in [validate(a, E3.CLAIMS, E3.LABELS, ev)]]
    return {"seeded_total": len(seeded), "caught": sum(s["caught"] for s in seeded),
            "caught_by_rules": sum(s["caught"] and s["by"].startswith("rules") for s in seeded),
            "good_total": len(good), "good_blocked": sum(g["blocked"] for g in good), "seeded": seeded, "good": good}


def mutate(a: dict, claims_by_id: dict, rng: random.Random) -> list[tuple[str, Assertion]]:
    t, cites, sec, sp = a["text"], list(a["cites"]), a["section"], a.get("speaker")
    out = []
    nums = [m for m in re.finditer(r"\d+(?:\.\d+)?", t) if not re.fullmatch(r"(19|20)\d\d", m.group())]
    if nums:
        m = nums[0]
        v = m.group()
        new = str(round(float(v) * 1.5 + 3, 1)).rstrip("0").rstrip(".") if "." in v else str(int(v) + 7)
        out.append(("wrong number", t[:m.start()] + new + t[m.end():]))
    mo = [x for x in MONTHS if re.search(rf"\b{x}\b", t)]
    if mo:
        other = MONTHS[(MONTHS.index(mo[0]) + 2) % 12]
        out.append(("wrong date", re.sub(rf"\b{mo[0]}\b", other, t, count=1)))
    if sp and sp in t:
        out.append(("wrong speaker", t.replace(sp, "Iran's foreign ministry", 1)))
    for x, y in (("fell", "rose"), ("rose", "fell"), ("increased", "decreased"), ("normally", "abnormally"),
                 ("open", "closed"), ("added", "removed"), ("deployed", "withdrew")):
        if re.search(rf"\b{x}\b", t):
            out.append(("direction flipped", re.sub(rf"\b{x}\b", y, t, count=1)))
            break
    m = re.search(r"\b(is|are|was|were|has|have|had)\b", t)
    if m:
        out.append(("negation", t[:m.end()] + " not" + t[m.end():]))
    out.append(("unsupported clause", t.rstrip(".") + ", after a drone attack on two tankers near Khasab."))
    if re.search(r",? according to [^,.;]+", t):
        out.append(("attribution removed", re.sub(r",? according to [^,.;]+", "", t, count=1)))
    if sec in ("key_findings", "by_the_numbers"):
        out.append(("forecast wording", t.rstrip(".") + ", and it will continue next week."))
    out.append(("citation dropped", None))
    res = []
    for kind, text in out:
        if kind == "citation dropped":
            res.append((kind, Assertion(a["n"], sec, t, [], sp)))
        elif text != t:
            res.append((kind, Assertion(a["n"], sec, text, cites, sp)))
    return res


def set_b() -> dict:
    prepare()
    client, key = client_and_key()
    rid = run_report(client, key, dict(HORMUZ, purpose="eval"))
    with pool().connection() as c:
        label_out = c.execute("SELECT output_ref FROM report_stages WHERE report_id=%s AND stage='label'", (rid,)).fetchone()
        rows = c.execute("SELECT assertion_no AS n, section, text, cited_claim_ids AS cites FROM report_assertions "
                         "WHERE report_id=%s AND result <> 'removed' ORDER BY assertion_no", (rid,)).fetchall()
    import json
    claims = labelled_claims(rid, json.loads(label_out["output_ref"]))
    by_id = {c["id"]: c for c in claims}
    cmap, lmap = rule_inputs(claims)

    def ev(cites):
        out = []
        for i in cites:
            if i in by_id:
                out += [by_id[i]["text"], *by_id[i]["passages"]] + ([f"Source: {by_id[i]['outlet']}"] if by_id[i]["outlet"] else [])
        return out
    from masader.simulated.heuristics import SPEAKER_START
    rng = random.Random(7)
    good, seeded = [], []
    for r in rows:
        m = SPEAKER_START.match(r["text"])
        a = {"n": r["n"], "section": r["section"], "text": r["text"], "cites": list(r["cites"]), "speaker": m.group(1) if m else None}
        blocked, why = validate(Assertion(a["n"], a["section"], a["text"], a["cites"], a["speaker"]), cmap, lmap, ev)
        good.append({"n": a["n"], "text": a["text"], "blocked": blocked, "by": why})
        for kind, mut in mutate(a, by_id, rng):
            # the pipeline's splitter derives the speaker from the sentence text, so do the same here
            ms = SPEAKER_START.match(mut.text)
            mut = Assertion(mut.n, mut.section, mut.text, mut.cited, ms.group(1) if ms else None)
            caught, why = validate(mut, cmap, lmap, ev)
            seeded.append({"type": kind, "n": a["n"], "text": mut.text, "caught": caught, "by": why})
    by_type: dict[str, list[int]] = {}
    for s in seeded:
        d = by_type.setdefault(s["type"], [0, 0])
        d[0] += 1
        d[1] += s["caught"]
    return {"report_id": rid, "assertions": len(rows), "seeded_total": len(seeded), "caught": sum(s["caught"] for s in seeded),
            "caught_by_rules": sum(s["caught"] and s["by"].startswith("rules") for s in seeded),
            "by_type": {k: f"{v[1]}/{v[0]}" for k, v in by_type.items()},
            "good_total": len(good), "good_blocked": sum(g["blocked"] for g in good), "seeded": seeded, "good": good}


def wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return [round((c - h) / d, 3), round((c + h) / d, 3)]


def main() -> dict:
    a, b = set_a(), set_b()
    for s in (a, b):
        s["detection_rate"] = round(s["caught"] / s["seeded_total"], 3)
        s["detection_ci95_wilson"] = wilson(s["caught"], s["seeded_total"])
        s["false_block_rate"] = round(s["good_blocked"] / s["good_total"], 3)
    out = {"what": "seeded-error detection: rules + SIMULATED lexical support check (not Claude)", "target": ">= 0.95",
           "set_a_e3_handwritten": a, "set_b_generated_from_pipeline_report": b}
    out["investigation"] = [
        "Fixed before the reported run (bugs, not tuning): set-B mutations kept the original speaker field while the "
        "text named a new speaker; the pipeline derives the speaker from the text, so the eval now does too.",
        "Fixed: the support check's pass reason claimed every word was present even when up to 30% was absent.",
        "Known limitation, not fixed: an added unsupported clause of about 6 content words in a 20+ word sentence "
        "stays under the 30% absent-word tolerance of the lexical check.",
        "Known limitation, not fixed: the forecast-wording rule exempts sentences with attribution ('said', "
        "'according to'), so an unattributed forecast appended to an attributed sentence passes.",
        "Known limitation, not fixed: the direction check has no deployed/withdrew pair.",
    ]
    save("seeded_errors", out)
    print({k: (v["caught"], v["seeded_total"], v["good_blocked"], v["good_total"]) for k, v in out.items() if isinstance(v, dict)})
    if "by_type" in b:
        print(b["by_type"])
    return out


if __name__ == "__main__":
    main()
