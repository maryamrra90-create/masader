"""AI-quality regression guards over the evaluation functions (eval/). The thresholds are the results
measured when the evaluation was first run (eval/results/*.json), so a change that makes grouping, the
evidence rules or the validator worse fails here. They are floors, not the proposal's targets: the
seeded-error target (>= 95%) is NOT met by the simulated support check, and these tests do not claim it is."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))

import quality  # noqa: E402
import seeded_errors  # noqa: E402


def test_grouping_never_joins_different_stories_and_finds_all_same_language_copies():
    d = quality.dedup()
    assert d["all_pairs"]["fp"] == 0
    assert d["by_language_pair"]["same_language"]["recall"] == 1.0
    assert d["all_pairs"]["recall"] >= 0.636            # measured; cross-language pairs need the multilingual embedder


def test_evidence_rules_label_all_a15_cases_correctly():
    a = quality.a15()
    assert a["correct"] == a["cases"] == 20


def test_validator_catches_all_handwritten_seeded_errors():
    a = seeded_errors.set_a()
    assert a["caught"] == a["seeded_total"] == 20
    assert a["good_blocked"] <= 3                        # measured 3/10: the lexical check over-blocks paraphrase
