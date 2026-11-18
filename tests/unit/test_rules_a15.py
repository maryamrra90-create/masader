"""Proposal A15: the evidence rules on 20 constructed claims (must be 100%), and the rule layer of the
output validator on the E3 good / seeded / mistyped assertions."""
import pytest

from eval import cases_e3 as E3
from masader.rules import RULE_VERSION, check_assertion, label_claim


def test_rule_version_is_recorded():
    assert RULE_VERSION == "rules-1.1.1"


@pytest.mark.parametrize("name,claim,evidence,expected", E3.A15, ids=[c[0] for c in E3.A15])
def test_a15_case(name, claim, evidence, expected):
    assert E3.check_a15(label_claim(claim, evidence), expected), name


@pytest.mark.parametrize("a", E3.GOOD, ids=[f"good-{g.n}" for g in E3.GOOD])
def test_correct_assertions_are_not_blocked(a):
    assert check_assertion(a, E3.CLAIMS, E3.LABELS) == []


def test_rules_catch_most_seeded_errors():
    caught = [t for t, a in E3.SEEDED if check_assertion(a, E3.CLAIMS, E3.LABELS)]
    assert len(caught) >= 17          # E3 measured 17/20 for the rule layer alone; the rest is the support check's job


def test_mistyped_sentences_are_checked_like_facts():
    """The typing is ignored: same result as E3 (19/20; the miss is the 'new claim with a real citation'
    case, which only the support check can catch)."""
    right = [bool(check_assertion(a, E3.CLAIMS, E3.LABELS)) == bad for a, bad in E3.MISTYPED]
    assert sum(right) == 19


def test_reason_wording_without_official_source():
    from datetime import datetime

    from masader.rules import Claim, Evidence
    lab = label_claim(Claim(1, "x", "fact"), [Evidence(1, 1, "B", "supports", False, datetime(2026, 10, 3), "p")])
    assert lab.reason.startswith("1 independent origin (best tier B); newest 3 October")
