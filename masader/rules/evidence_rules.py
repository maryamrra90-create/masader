"""Evidence labels, reliability reasons and the rule part of the output validator (rules-1.1.1).

Carried over unchanged from validation experiment E3 (architecture I.4), where the 20 A15 cases pass;
the same cases run as unit tests in tests/unit/test_rules_a15.py.

Pure functions with no I/O: the pipeline's `label` stage calls `label_claim` for every claim and the
`validate` stage calls `check_assertion` before the LLM support check. Interface (section C, "function
interfaces"):

    label_claim(claim: Claim, evidence: list[Evidence]) -> Label
    check_assertion(a: Assertion, claims: dict[int, Claim], labels: dict[int, Label]) -> list[str]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

RULE_VERSION = "rules-1.1.1"  # 1.1: reported speech may contain forecast words; "will" counts as forecast wording
#                               1.1.1: reason wording only (no stray comma without an official source); labels unchanged
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")
MONTHS = ("January February March April May June July August September October November December").split()


@dataclass
class Evidence:
    item_id: int
    origin_group: int
    tier: str                    # A, B, C from the source register
    verdict: str                 # supports, contradicts, not_addressed (from the Verifier)
    first_hand: bool             # publisher of the statement or figure
    published: datetime
    publisher: str
    value: str | None = None     # the figure as stated in this passage, if any
    superseded: bool = False     # a newer passage from the same publisher replaces it
    official: bool = False


@dataclass
class Claim:
    id: int
    text: str
    type: str                    # fact, attributed, opinion, forecast, other
    speaker: str | None = None
    party_to_dispute: bool = False   # figure given by a party about the other side
    passages: list[str] = field(default_factory=list)


@dataclass
class Label:
    label: str
    low_reliability: bool
    attribution_required: bool
    cite_items: list[int]
    reason: str
    replaced: list[int] = field(default_factory=list)   # secondhand figures replaced by the primary


def _mark_superseded(ev: list[Evidence]) -> list[Evidence]:
    """Within one publisher, a newer passage stating a different figure supersedes the older ones."""
    by_pub: dict[str, list[Evidence]] = {}
    for e in ev:
        by_pub.setdefault(e.publisher, []).append(e)
    for items in by_pub.values():
        items.sort(key=lambda e: e.published)
        newest = items[-1]
        for e in items[:-1]:
            if e.value is not None and newest.value is not None and e.value != newest.value:
                e.superseded = True
    return ev


def label_claim(claim: Claim, evidence: list[Evidence]) -> Label:
    ev = _mark_superseded([e for e in evidence])
    sup = [e for e in ev if e.verdict == "supports" and not e.superseded]
    con = [e for e in ev if e.verdict == "contradicts" and not e.superseded]
    sup_groups = {e.origin_group for e in sup}
    con_groups = {e.origin_group for e in con} - sup_groups      # only independent contradictions count
    attribution = claim.type == "attributed" or claim.party_to_dispute

    # primary over secondhand: cite the publisher of the figure when it is available
    primary = [e for e in sup if e.first_hand]
    replaced = []
    if primary:
        pval = primary[0].value
        replaced = [e.item_id for e in sup if not e.first_hand and e.value is not None and pval is not None and e.value != pval]
    cite = [e.item_id for e in (primary or sup) if e.item_id not in replaced]

    if not sup_groups:
        superseded = any(e.superseded and e.verdict == "supports" for e in ev)
        why = "only superseded figures support it" if superseded else "no cited passage supports it"
        return Label("insufficient", False, attribution, [], why)
    if con_groups:
        return Label("disputed", False, True, cite + [e.item_id for e in con],
                     f"{len(sup_groups)} supporting and {len(con_groups)} contradicting independent origins", replaced)
    best = min(e.tier for e in sup)                                 # 'A' < 'B' < 'C'
    newest = max(e.published for e in sup)
    if len(sup_groups) >= 2 and best in ("A", "B"):
        lab, low = "corroborated", False
    else:
        lab, low = "single_source", best == "C"
        attribution = True
    official = ", including an official statement" if any(e.official for e in sup) else ""
    reason = (f"{len(sup_groups)} independent origin{'s' if len(sup_groups) > 1 else ''}{official} (best tier {best}); "
              f"newest {newest.day} {newest.strftime('%B')}; no contradiction found")
    if low:
        reason += "; low reliability: only tier C sources"
    return Label(lab, low, attribution, cite, reason, replaced)


# ---------------- output validator: rule checks (steps 2, 3 and 5) ----------------
NUM = re.compile(r"\d+(?:[.,]\d+)?")
ATTRIB = re.compile(r"\b(according to|said|says|stated|reported|announced|told)\b", re.I)
HEDGE = re.compile(r"\b(may|might|could|likely|expected to|is set to|would|will)\b", re.I)


@dataclass
class Assertion:
    n: int
    section: str                 # key_findings, by_the_numbers, analysis, scenarios ...
    text: str
    cited: list[int]
    speaker: str | None = None
    typed_as: str = "fact"       # what the classifier or LLM said; ignored for evidence checks


def _numbers(s: str) -> set[str]:
    s = s.translate(ARABIC_DIGITS).replace(",", "")
    return {n.rstrip("0").rstrip(".") if "." in n else n for n in NUM.findall(s)}


def _months(s: str) -> set[str]:
    # capitalised month names only, so the hedge word "may" is not read as a date
    return {m for m in MONTHS if re.search(rf"\b{m}\b", s)}


def check_assertion(a: Assertion, claims: dict[int, Claim], labels: dict[int, Label]) -> list[str]:
    """Return the reasons the assertion fails; [] means it passes to the LLM support check."""
    fails = []
    cited = [claims[i] for i in a.cited if i in claims]
    if not cited:                                                    # step 2
        return ["no_citation"]
    support_text = " ".join([c.text for c in cited] + [p for c in cited for p in c.passages])
    extra_nums = _numbers(a.text) - _numbers(support_text)          # step 3
    if extra_nums:
        fails.append(f"number_not_in_evidence:{sorted(extra_nums)}")
    extra_months = _months(a.text) - _months(support_text)
    if extra_months:
        fails.append(f"date_not_in_evidence:{sorted(extra_months)}")
    speakers = {c.speaker for c in cited if c.speaker}
    if a.speaker and speakers and a.speaker not in speakers:
        fails.append("speaker_mismatch")
    for i in a.cited:                                                # step 5: wording must match the label
        lab = labels.get(i)
        if lab is None:
            continue
        if lab.label == "insufficient":
            fails.append("cites_insufficient_claim")
        if lab.attribution_required and not ATTRIB.search(a.text):
            fails.append("attribution_missing")
        if lab.label == "disputed" and len(a.cited) < 2:
            fails.append("disputed_without_other_version")
    if a.section in ("key_findings", "by_the_numbers") and HEDGE.search(a.text) and not ATTRIB.search(a.text):
        fails.append("forecast_wording_in_findings")
    return sorted(set(fails))
