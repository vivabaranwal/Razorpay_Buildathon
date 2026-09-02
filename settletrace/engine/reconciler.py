"""Settlement reconciliation (FR-1.3, FR-1.5, FR-1.6).

The governing invariant, from SRS 4.3.2 and repeated as business rule 1 in FRS
section 10: no transaction may be silently dropped. Every input row leaves this
engine either verified or attached to an exception. ``ReconciliationResult``
asserts that before it is returned, so a future change that loses a row fails
loudly here instead of quietly under-reporting a merchant's discrepancies.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..config import Rules
from ..models import MatchStatus, ReasonCode
from .fees import compute_expected_fees, verify_fees


@dataclass
class MatchOutcome:
    """Reconciliation result for a single transaction."""

    transaction_id: str
    settlement_id: str
    match_status: MatchStatus
    expected_net_paise: int
    actual_net_paise: int
    # A transaction can fail more than one check - an unexpected MDR rate also
    # throws off the GST computed from it - so exceptions are a list.
    exceptions: list[dict] = field(default_factory=list)


@dataclass
class ReconciliationResult:
    """Batch-level outcome (FR-1.6)."""

    settlement_id: str
    outcomes: list[MatchOutcome]
    settlement_total_exception: dict | None = None

    def __post_init__(self) -> None:
        self._assert_no_silent_drops()

    def _assert_no_silent_drops(self) -> None:
        accounted = self.verified_count + self.exception_count
        if accounted != len(self.outcomes):
            raise AssertionError(
                "Reconciliation lost rows: "
                f"{len(self.outcomes)} processed but {accounted} accounted for. "
                "Every transaction must be verified or excepted (SRS 4.3.2)."
            )

    @property
    def transactions_processed(self) -> int:
        return len(self.outcomes)

    @property
    def verified_count(self) -> int:
        return sum(1 for o in self.outcomes if o.match_status is MatchStatus.VERIFIED)

    @property
    def exception_count(self) -> int:
        return sum(1 for o in self.outcomes if o.match_status is MatchStatus.EXCEPTION)

    @property
    def accuracy(self) -> float:
        """Fraction auto-matched. An empty batch is 0.0, never 1.0.

        Reporting a vacuous 100% for a batch that reconciled nothing would be
        exactly the false 'all clear' PRD 3.2 says merchants must never be given.
        """
        if not self.outcomes:
            return 0.0
        return self.verified_count / len(self.outcomes)

    def all_exceptions(self) -> list[dict]:
        rows = [exc for outcome in self.outcomes for exc in outcome.exceptions]
        if self.settlement_total_exception:
            rows.append(self.settlement_total_exception)
        return rows


def reconcile_settlement(
    transactions: pd.DataFrame,
    settlement: dict,
    line_items: pd.DataFrame,
    rules: Rules,
    merchant: str = "default",
) -> ReconciliationResult:
    """Reconcile one settlement against its transactions.

    ``transactions`` carries the columns transaction_id, amount_paise, method,
    status and settlement_id; ``line_items`` carries transaction_id, fee_paise,
    gst_paise and reserve_paise as Razorpay reported them.
    """
    settlement_id = settlement["settlement_id"]

    # Index the actual deductions once. Reconciling a large settlement is
    # otherwise a linear scan per transaction, which is what turns a batch of a
    # few thousand rows into the quadratic blowup NFR 4.3.1's 10-second budget
    # cannot absorb.
    if line_items.empty:
        actuals: dict[str, dict] = {}
    else:
        actuals = line_items.set_index("transaction_id").to_dict("index")

    outcomes: list[MatchOutcome] = []
    matched_net_total = 0

    for txn in transactions.to_dict("records"):
        outcome = _reconcile_one(txn, settlement_id, actuals, rules, merchant)
        outcomes.append(outcome)
        if outcome.match_status is MatchStatus.VERIFIED:
            matched_net_total += outcome.actual_net_paise

    total_exception = _verify_settlement_total(
        settlement_id=settlement_id,
        computed_total=matched_net_total,
        reported_total=settlement["amount_paise"],
        rules=rules,
    )

    return ReconciliationResult(
        settlement_id=settlement_id,
        outcomes=outcomes,
        settlement_total_exception=total_exception,
    )


def _reconcile_one(
    txn: dict,
    settlement_id: str,
    actuals: dict[str, dict],
    rules: Rules,
    merchant: str,
) -> MatchOutcome:
    """Reconcile a single transaction against the settlement under audit."""
    txn_id = txn["transaction_id"]
    amount = int(txn["amount_paise"])
    exceptions: list[dict] = []

    # FR-1.3: is this transaction claimed by the settlement being audited?
    # A missing reference arrives from pandas as NaN rather than None, so it is
    # normalised here instead of relying on NaN's inequality to carry the logic.
    txn_settlement = txn.get("settlement_id")
    if txn_settlement is not None and pd.isna(txn_settlement):
        txn_settlement = None
    claimed_by_settlement = txn_settlement == settlement_id
    line_item = actuals.get(txn_id)

    if not claimed_by_settlement or line_item is None:
        # A captured payment in the window that no line item accounts for is the
        # settlement-opacity case itself: money the merchant earned that this
        # payout does not explain. An uncaptured one is merely still in flight,
        # which is a different situation and gets its own reason code so a
        # reviewer is not sent chasing a payment that is behaving normally.
        status = str(txn.get("status", ""))
        reason = (
            ReasonCode.LATE_AUTHORIZATION_PENDING
            if status in {"authorized", "pending", "created"}
            else ReasonCode.UNMATCHED_TRANSACTION
        )
        exceptions.append(
            _exception_row(
                reason_code=reason,
                transaction_id=txn_id,
                settlement_id=settlement_id,
                expected=amount,
                actual=0,
            )
        )
        return MatchOutcome(
            transaction_id=txn_id,
            settlement_id=settlement_id,
            match_status=MatchStatus.EXCEPTION,
            expected_net_paise=amount,
            actual_net_paise=0,
            exceptions=exceptions,
        )

    # FR-1.4: recompute the deduction independently and compare.
    expected = compute_expected_fees(amount, str(txn["method"]), rules, merchant)
    actual_fee = int(line_item.get("fee_paise", 0))
    actual_gst = int(line_item.get("gst_paise", 0))
    actual_reserve = int(line_item.get("reserve_paise", 0))

    discrepancies = verify_fees(
        expected, actual_fee, actual_gst, actual_reserve, rules
    )
    exceptions.extend(
        _exception_row(
            reason_code=d.reason_code,
            transaction_id=txn_id,
            settlement_id=settlement_id,
            expected=d.expected_paise,
            actual=d.actual_paise,
        )
        for d in discrepancies
    )

    expected_net = amount - expected.total_paise
    actual_net = amount - (actual_fee + actual_gst + actual_reserve)

    return MatchOutcome(
        transaction_id=txn_id,
        settlement_id=settlement_id,
        match_status=MatchStatus.EXCEPTION if exceptions else MatchStatus.VERIFIED,
        expected_net_paise=expected_net,
        actual_net_paise=actual_net,
        exceptions=exceptions,
    )


def _verify_settlement_total(
    settlement_id: str,
    computed_total: int,
    reported_total: int,
    rules: Rules,
) -> dict | None:
    """Check the sum of verified nets against the payout Razorpay reported.

    This catches what per-transaction checks structurally cannot: a payout whose
    individual line items each look correct but which does not add up to the
    money that reached the bank.
    """
    delta = reported_total - computed_total
    if abs(delta) <= rules.match_tolerance_paise:
        return None

    return _exception_row(
        reason_code=ReasonCode.SETTLEMENT_TOTAL_MISMATCH,
        transaction_id=None,
        settlement_id=settlement_id,
        expected=computed_total,
        actual=reported_total,
    )


def _exception_row(
    reason_code: ReasonCode,
    transaction_id: str | None,
    settlement_id: str,
    expected: int,
    actual: int,
) -> dict:
    return {
        "reason_code": reason_code,
        "transaction_id": transaction_id,
        "settlement_id": settlement_id,
        "expected_paise": expected,
        "actual_paise": actual,
        "delta_paise": actual - expected,
    }
