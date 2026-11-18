"""Evidence labels and output-validator rules: pure functions, no I/O (architecture ADR-09)."""
from .evidence_rules import RULE_VERSION, Assertion, Claim, Evidence, Label, check_assertion, label_claim

__all__ = ["RULE_VERSION", "Assertion", "Claim", "Evidence", "Label", "check_assertion", "label_claim"]
