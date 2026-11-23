"""The rule test cases of validation experiment E3, carried over unchanged (architecture I.4):

  * A15: 20 constructed claims, one evidence rule each, with the expected label (proposal A15);
  * GOOD: 10 correct brief assertions that the validator must not block;
  * SEEDED: 20 assertions with one seeded error each (wrong number, date, speaker, new claim, false fact
    inside analysis, overstated certainty, changed meaning);
  * MISTYPED: 20 factual sentences mistyped as opinion or forecast, which must still be checked.

They were written by hand from the rules in proposal section 6 and A12. Note for any result computed on
them: the simulated support check was written knowing these cases, so its score on SEEDED is optimistic;
eval/seeded_errors.py therefore also reports a second, generated set.
"""
from datetime import datetime as D

from masader.rules import Assertion, Claim, Evidence, check_assertion, label_claim  # noqa: F401


def E(item, group, tier, verdict="supports", first=False, day=18, pub=None, value=None, official=False):
    return Evidence(item, group, tier, verdict, first, D(2026, 10, day, 9, 0), pub or f"p{item}", value, official=official)


C = lambda i, t="fact", **k: Claim(i, f"claim {i}", t, **k)  # noqa: E731

# ---------- A15: 20 constructed claims, one rule each, with the expected outcome ----------
A15 = [
    ("two independent tier A/B origins", C(1), [E(1, 1, "A"), E(2, 2, "B")], {"label": "corroborated"}),
    ("syndicated copies are one origin", C(2), [E(3, 5, "A"), E(4, 5, "B"), E(5, 5, "B")], {"label": "single_source"}),
    ("20 wire copies still one origin", C(3), [E(10 + i, 7, "B") for i in range(20)], {"label": "single_source"}),
    ("only tier C support is flagged", C(4), [E(6, 8, "C"), E(7, 9, "C")], {"label": "single_source", "low_reliability": True}),
    ("one tier C plus one tier B corroborates", C(5), [E(8, 10, "C"), E(9, 11, "B")], {"label": "corroborated"}),
    ("independent contradiction disputes", C(6), [E(30, 12, "A"), E(31, 13, "B", "contradicts")], {"label": "disputed"}),
    ("contradiction inside the same origin group is not independent", C(7), [E(32, 14, "A"), E(33, 15, "B"), E(34, 14, "B", "contradicts")], {"label": "corroborated"}),
    ("quote present but not supporting", C(8), [E(35, 16, "A", "not_addressed"), E(36, 17, "B", "not_addressed")], {"label": "insufficient"}),
    ("no evidence at all", C(9), [], {"label": "insufficient"}),
    ("superseded figure only -> insufficient", C(10), [E(37, 18, "A", day=10, pub="worldbank", value="4.1"),
                                                       E(38, 18, "A", "not_addressed", day=20, pub="worldbank", value="3.6")], {"label": "insufficient"}),
    ("newest figure from same publisher is used", C(11), [E(39, 19, "A", day=10, pub="cbuae", value="2.1"),
                                                           E(40, 19, "A", day=20, pub="cbuae", value="2.4"), E(41, 20, "B", value="2.4")],
     {"label": "corroborated", "cite_excludes": [39]}),
    ("primary cited over secondhand", C(12, "attributed", speaker="Ministry"), [E(42, 21, "A", first=True, value="5"), E(43, 22, "B", value="5")],
     {"cite_first": 42, "attribution_required": True}),
    ("secondhand figure that differs from primary is replaced", C(13), [E(44, 23, "A", first=True, value="12"), E(45, 24, "B", value="15")],
     {"replaced": [45]}),
    ("party to a dispute is always attributed", C(14, party_to_dispute=True), [E(46, 25, "A", first=True), E(47, 26, "B")],
     {"attribution_required": True}),
    ("state outlet on its own government's statement is first-hand, still single origin", C(15, "attributed", speaker="Government"),
     [E(48, 27, "A", first=True, official=True)], {"label": "single_source", "attribution_required": True}),
    ("single-source always needs attribution", C(16), [E(49, 28, "A")], {"label": "single_source", "attribution_required": True}),
    ("reason names origins and newest date", C(17), [E(50, 29, "A", day=12, official=True), E(51, 30, "B", day=18)],
     {"reason_contains": ["2 independent origins", "official statement", "newest 18 October"]}),
    ("disputed shows both versions", C(18), [E(52, 31, "B"), E(53, 32, "A", "contradicts")], {"label": "disputed", "cite_includes": [52, 53]}),
    ("tier C contradiction still disputes", C(19), [E(54, 33, "A"), E(55, 34, "B"), E(56, 35, "C", "contradicts")], {"label": "disputed"}),
    ("allegation needs two independent origins to corroborate", C(20, "attributed", speaker="Ministry"), [E(57, 36, "A")],
     {"label": "single_source"}),
]


def check_a15(lab, exp):
    ok = True
    for k, v in exp.items():
        if k == "cite_excludes":
            ok &= not set(v) & set(lab.cite_items)
        elif k == "cite_includes":
            ok &= set(v) <= set(lab.cite_items)
        elif k == "cite_first":
            ok &= bool(lab.cite_items) and lab.cite_items[0] == v
        elif k == "reason_contains":
            ok &= all(s in lab.reason for s in v)
        else:
            ok &= getattr(lab, k) == v
    return ok


# ---------- validator fixtures ----------
CLAIMS = {
    1: Claim(1, "Tanker transits through the Strait of Hormuz fell 18% in the week to 18 October.", "fact",
             passages=["tanker transits fell 18 percent in the week to October 18"]),
    2: Claim(2, "Oman's foreign ministry said talks with Iran on navigation would resume on 25 October.", "attributed",
             speaker="Oman's foreign ministry", passages=["the ministry said talks would resume on October 25"]),
    3: Claim(3, "India's shipping ministry issued an advisory to Indian-flagged vessels on 16 October.", "fact",
             passages=["the advisory was issued on 16 October to Indian-flagged vessels"]),
    4: Claim(4, "Insurers raised war-risk premiums to 0.7% of hull value.", "fact", passages=["war-risk premiums rose to 0.7% of hull value"]),
    5: Claim(5, "UAE GDP was US$537 billion in 2024 (World Bank, as of 1 July 2025).", "fact",
             passages=["GDP (current US$) 2024: 537,079,000,000"]),
    6: Claim(6, "A port official said 40 vessels were waiting at anchor.", "attributed", speaker="port official",
             passages=["40 vessels were waiting at anchor, a port official said"]),
    7: Claim(7, "Another outlet reported 25 vessels waiting.", "attributed", speaker="Gulf Times", passages=["25 vessels were waiting"]),
}
LABELS = {
    1: label_claim(CLAIMS[1], [E(1, 1, "A"), E(2, 2, "B")]),
    2: label_claim(CLAIMS[2], [E(3, 3, "A", first=True)]),
    3: label_claim(CLAIMS[3], [E(4, 4, "A"), E(5, 5, "B")]),
    4: label_claim(CLAIMS[4], [E(6, 6, "B"), E(7, 7, "B")]),
    5: label_claim(CLAIMS[5], [E(8, 8, "A", first=True), E(9, 9, "B")]),
    6: label_claim(CLAIMS[6], [E(10, 10, "B"), E(11, 11, "A", "contradicts")]),
    7: label_claim(CLAIMS[7], [E(11, 11, "A"), E(10, 10, "B", "contradicts")]),
}
K, N, A_ = "key_findings", "by_the_numbers", "analysis"

GOOD = [  # correct assertions: must pass the rule layer (false-block check)
    Assertion(1, K, "Tanker transits through the Strait of Hormuz fell 18% in the week to 18 October.", [1]),
    Assertion(2, K, "Oman's foreign ministry said talks on navigation would resume on 25 October.", [2], speaker="Oman's foreign ministry"),
    Assertion(3, K, "India issued an advisory to Indian-flagged vessels on 16 October.", [3]),
    Assertion(4, N, "War-risk premiums: 0.7% of hull value.", [4]),
    Assertion(5, N, "UAE GDP: US$537 billion (2024, World Bank, as of 1 July 2025).", [5]),
    Assertion(6, K, "A port official said 40 vessels were waiting at anchor, while another outlet reported 25.", [6, 7], speaker="port official"),
    Assertion(7, A_, "Because transits fell 18%, freight rates may rise further.", [1]),
    Assertion(8, A_, "With premiums at 0.7% of hull value, smaller operators could pause sailings.", [4]),
    Assertion(9, K, "Transits fell 18% in the week to 18 October, and insurers raised premiums to 0.7%.", [1, 4]),
    Assertion(10, A_, "If talks resume on 25 October, as the ministry said, traffic could recover.", [2], speaker="Oman's foreign ministry"),
]
SEEDED = [  # (type, assertion): every one contains an unsupported assertion
    ("wrong number", Assertion(101, K, "Tanker transits fell 28% in the week to 18 October.", [1])),
    ("wrong number", Assertion(102, N, "War-risk premiums: 1.7% of hull value.", [4])),
    ("wrong number", Assertion(103, N, "UAE GDP: US$573 billion (2024).", [5])),
    ("wrong number", Assertion(104, K, "A port official said 400 vessels were waiting, while another outlet reported 25.", [6, 7], speaker="port official")),
    ("wrong date", Assertion(105, K, "India issued an advisory to Indian-flagged vessels on 16 September.", [3])),
    ("wrong date", Assertion(106, K, "Oman's foreign ministry said talks would resume on 25 November.", [2], speaker="Oman's foreign ministry")),
    ("wrong date", Assertion(107, K, "Tanker transits fell 18% in the week to 18 August.", [1])),
    ("new claim, no citation", Assertion(108, K, "The US Navy began escorting tankers on 20 October.", [])),
    ("new claim, no citation", Assertion(109, A_, "Because Qatar halted LNG loadings, prices may rise.", [])),
    ("new claim with real citation", Assertion(110, K, "Tanker transits fell 18% in the week to 18 October after a drone attack.", [1])),
    ("wrong speaker", Assertion(111, K, "Iran's foreign ministry said talks would resume on 25 October.", [2], speaker="Iran's foreign ministry")),
    ("wrong speaker", Assertion(112, K, "A shipping company said 40 vessels were waiting, while another outlet reported 25.", [6, 7], speaker="shipping company")),
    ("false fact inside analysis", Assertion(113, A_, "Because transits fell 35%, freight rates may rise further.", [1])),
    ("false fact inside analysis", Assertion(114, A_, "With premiums at 0.7% and GDP at US$600 billion, growth could slow.", [4, 5])),
    ("false fact inside analysis", Assertion(115, A_, "Since India's advisory on 12 October, Indian-flagged traffic may fall.", [3])),
    ("overstated certainty", Assertion(116, K, "Talks on navigation will resume on 25 October.", [2])),
    ("overstated certainty", Assertion(117, K, "40 vessels were waiting at anchor.", [6])),
    ("overstated certainty", Assertion(118, K, "Transits fell 18% and will keep falling next week.", [1])),
    ("meaning changed, same numbers", Assertion(119, K, "Tanker transits rose 18% in the week to 18 October.", [1])),
    ("meaning changed, same numbers", Assertion(120, K, "Insurers cut war-risk premiums to 0.7% of hull value.", [4])),
]
# 20 sentences typed as opinion or forecast that are factual: half wrong, half right; all must be checked
MISTYPED = [(Assertion(200 + i, K, a.text, a.cited, a.speaker, typed_as="opinion"), True) for i, (_, a) in enumerate(SEEDED[:10])] + \
           [(Assertion(220 + i, K if g.section != A_ else A_, g.text, g.cited, g.speaker, typed_as="forecast"), False) for i, g in enumerate(GOOD)]
