"""Detection quality against a labelled batch (FR-1.6).

A single accuracy percentage cannot distinguish "found every defect" from
"flagged everything indiscriminately" - a system that raised an exception on
every row would score 100% recall and be useless. FR-1.6 therefore asks for
precision and recall against a batch whose correct answer is independently
known, which is what this module computes.

Scoring is per (transaction, reason code) pair rather than per transaction. A
transaction flagged for the *wrong* reason has not really been detected: the
merchant is sent to look at a fee dispute when the real problem is a missing
payment, so counting it as a hit would overstate the engine's usefulness.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import ReasonCode

# Findings the engine reports that ground truth cannot speak to. The generator
# plants defects on individual transactions; a settlement-level total mismatch
# is an arithmetic consequence of those defects rather than a separate planted
# fault, so scoring it as a false positive would penalise the engine for
# correctly reporting a payout that genuinely does not add up.
_UNLABELLED_FINDINGS = {ReasonCode.SETTLEMENT_TOTAL_MISMATCH}


@dataclass(frozen=True)
class DetectionMetrics:
    """Precision and recall for one batch, with the raw counts behind them."""

    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        """Of everything flagged, the fraction that was a real planted defect.

        A batch with nothing flagged scores 1.0: it raised no false alarms,
        which is what precision measures. Recall is the term that penalises
        having missed the defects.
        """
        flagged = self.true_positives + self.false_positives
        if flagged == 0:
            return 1.0
        return self.true_positives / flagged

    @property
    def recall(self) -> float:
        """Of every planted defect, the fraction the engine found.

        A batch with nothing planted scores 1.0 - there was nothing to miss.
        """
        planted = self.true_positives + self.false_negatives
        if planted == 0:
            return 1.0
        return self.true_positives / planted

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        if total == 0:
            return 0.0
        return 2 * self.precision * self.recall / total

    @property
    def is_perfect(self) -> bool:
        return self.false_positives == 0 and self.false_negatives == 0


def evaluate_detection(
    detected: list[tuple[str | None, ReasonCode]],
    injected: list[tuple[str, ReasonCode]],
) -> DetectionMetrics:
    """Score detected exceptions against the batch's known planted defects.

    Both arguments are ``(transaction_id, reason_code)`` pairs. Duplicates are
    collapsed: the engine can raise two exceptions on one transaction (a wrong
    MDR rate also throws off the GST computed from it), and each distinct
    finding should be scored once rather than inflating the counts.
    """
    detected_set = {
        (txn_id, reason)
        for txn_id, reason in detected
        if txn_id is not None and reason not in _UNLABELLED_FINDINGS
    }
    injected_set = set(injected)

    true_positives = detected_set & injected_set

    return DetectionMetrics(
        true_positives=len(true_positives),
        false_positives=len(detected_set - injected_set),
        false_negatives=len(injected_set - detected_set),
    )
