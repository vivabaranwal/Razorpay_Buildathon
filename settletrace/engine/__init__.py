"""Deterministic reconciliation engine.

No module in this package imports the web framework, the database, or the LLM
layer. The reconciliation decision is meant to be reproducible from its inputs
alone, and a dependency on any of those would make it a property of the running
system rather than of the numbers.
"""

from .fees import compute_expected_fees, verify_fees
from .metrics import DetectionMetrics, evaluate_detection
from .reconciler import MatchOutcome, ReconciliationResult, reconcile_settlement

__all__ = [
    "DetectionMetrics",
    "MatchOutcome",
    "ReconciliationResult",
    "compute_expected_fees",
    "evaluate_detection",
    "reconcile_settlement",
    "verify_fees",
]
