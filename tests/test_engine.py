"""Tests for the deterministic reconciliation engine.

These cover the acceptance criteria stated in the FRS for FR-1.3 through FR-1.6.
"""

from __future__ import annotations

import pandas as pd

from settletrace.config import Rules
from settletrace.engine.fees import compute_expected_fees, verify_fees
from settletrace.engine.reconciler import reconcile_settlement
from settletrace.models import MatchStatus, ReasonCode


def _settlement(amount_paise: int, settlement_id: str = "setl_001") -> dict:
    return {"settlement_id": settlement_id, "amount_paise": amount_paise}


def _txn(txn_id: str, amount: int, method: str = "card", **kw) -> dict:
    row = {
        "transaction_id": txn_id,
        "amount_paise": amount,
        "method": method,
        "status": "captured",
        "settlement_id": "setl_001",
    }
    row.update(kw)
    return row


def _line_item(txn_id: str, fee: int, gst: int, reserve: int = 0) -> dict:
    return {
        "transaction_id": txn_id,
        "fee_paise": fee,
        "gst_paise": gst,
        "reserve_paise": reserve,
    }


class TestFeeComputation:
    def test_card_fee_and_gst(self, rules: Rules) -> None:
        # INR 1000.00 = 100000 paise at 2% MDR -> 2000 paise fee, 360 paise GST.
        expected = compute_expected_fees(100_000, "card", rules)
        assert expected.fee_paise == 2_000
        assert expected.gst_paise == 360
        assert expected.total_paise == 2_360

    def test_upi_is_zero_mdr(self, rules: Rules) -> None:
        expected = compute_expected_fees(100_000, "upi", rules)
        assert expected.fee_paise == 0
        assert expected.gst_paise == 0

    def test_unknown_method_falls_back_to_card_not_zero(self, rules: Rules) -> None:
        """An unknown method must not silently price as free.

        Treating it as zero would make a real deduction look like an overcharge
        and bury the actual problem (an unmapped method) under a fee exception.
        """
        expected = compute_expected_fees(100_000, "some_new_method", rules)
        assert expected.fee_paise == 2_000

    def test_half_paise_rounds_up_not_to_even(self, rules: Rules) -> None:
        """Guards the ROUND_HALF_UP choice against a drift back to round()."""
        # 12525 paise x 2% = 250.5 paise -> 251 under half-up, 250 under banker's.
        expected = compute_expected_fees(12_525, "card", rules)
        assert expected.fee_paise == 251

    def test_matching_fees_produce_no_discrepancy(self, rules: Rules) -> None:
        expected = compute_expected_fees(100_000, "card", rules)
        assert verify_fees(expected, 2_000, 360, 0, rules) == []

    def test_fee_and_gst_flagged_separately(self, rules: Rules) -> None:
        expected = compute_expected_fees(100_000, "card", rules)
        found = verify_fees(expected, 2_500, 999, 0, rules)
        assert {d.reason_code for d in found} == {
            ReasonCode.FEE_MISMATCH,
            ReasonCode.GST_MISMATCH,
        }

    def test_opposing_errors_do_not_cancel(self, rules: Rules) -> None:
        """Per-component checks stop a net-zero total from passing.

        Fee overcharged by 500 and GST undercharged by 500 sums to zero; a
        total-only check would call this clean.
        """
        expected = compute_expected_fees(100_000, "card", rules)
        found = verify_fees(expected, 2_500, 0, 0, rules)
        assert len(found) == 2


class TestReconciliation:
    def test_clean_batch_is_fully_verified(self, rules: Rules) -> None:
        """FR-1.3 acceptance: correctly tagged transactions all match."""
        txns = pd.DataFrame([_txn("pay_1", 100_000), _txn("pay_2", 50_000)])
        items = pd.DataFrame(
            [_line_item("pay_1", 2_000, 360), _line_item("pay_2", 1_000, 180)]
        )
        # Net = (100000 - 2360) + (50000 - 1180) = 97640 + 48820 = 146460
        result = reconcile_settlement(txns, _settlement(146_460), items, rules)

        assert result.transactions_processed == 2
        assert result.verified_count == 2
        assert result.accuracy == 1.0
        assert result.all_exceptions() == []

    def test_injected_mismatches_are_all_flagged(self, rules: Rules) -> None:
        """FR-1.5 acceptance: N injected mismatches yield exactly N exceptions."""
        txns = pd.DataFrame(
            [_txn("pay_1", 100_000), _txn("pay_2", 50_000), _txn("pay_3", 20_000)]
        )
        items = pd.DataFrame(
            [
                _line_item("pay_1", 2_000, 360),      # correct
                _line_item("pay_2", 1_500, 180),      # fee overcharged by 500
                _line_item("pay_3", 400, 72),         # correct
            ]
        )
        result = reconcile_settlement(txns, _settlement(999_999_999), items, rules)

        fee_excs = [
            e
            for e in result.all_exceptions()
            if e["reason_code"] is ReasonCode.FEE_MISMATCH
        ]
        assert len(fee_excs) == 1
        assert fee_excs[0]["transaction_id"] == "pay_2"
        assert fee_excs[0]["delta_paise"] == 500  # positive: merchant overcharged

    def test_unmatched_transaction_is_surfaced(self, rules: Rules) -> None:
        """A captured payment with no line item is the opacity case itself."""
        txns = pd.DataFrame([_txn("pay_1", 100_000), _txn("pay_orphan", 75_000)])
        items = pd.DataFrame([_line_item("pay_1", 2_000, 360)])
        result = reconcile_settlement(txns, _settlement(97_640), items, rules)

        orphans = [
            e
            for e in result.all_exceptions()
            if e["reason_code"] is ReasonCode.UNMATCHED_TRANSACTION
        ]
        assert len(orphans) == 1
        assert orphans[0]["transaction_id"] == "pay_orphan"

    def test_pending_payment_gets_its_own_reason_code(self, rules: Rules) -> None:
        """An in-flight payment is not the same defect as a missing one."""
        txns = pd.DataFrame([_txn("pay_late", 100_000, status="authorized")])
        result = reconcile_settlement(
            txns, _settlement(0), pd.DataFrame(), rules
        )
        codes = {e["reason_code"] for e in result.all_exceptions()}
        assert ReasonCode.LATE_AUTHORIZATION_PENDING in codes
        assert ReasonCode.UNMATCHED_TRANSACTION not in codes

    def test_settlement_total_mismatch_detected(self, rules: Rules) -> None:
        """Line items can each look right while the payout still does not add up."""
        txns = pd.DataFrame([_txn("pay_1", 100_000)])
        items = pd.DataFrame([_line_item("pay_1", 2_000, 360)])
        # Correct net is 97640; Razorpay reports 90000 - a 7640 paise shortfall.
        result = reconcile_settlement(txns, _settlement(90_000), items, rules)

        totals = [
            e
            for e in result.all_exceptions()
            if e["reason_code"] is ReasonCode.SETTLEMENT_TOTAL_MISMATCH
        ]
        assert len(totals) == 1
        assert totals[0]["delta_paise"] == -7_640

    def test_rounding_within_tolerance_is_not_an_exception(self, rules: Rules) -> None:
        """FR-1.3 business rule: tolerance is configurable, not zero."""
        txns = pd.DataFrame([_txn("pay_1", 100_000)])
        items = pd.DataFrame([_line_item("pay_1", 2_000, 360)])
        # 50 paise off, inside the 100-paise batch tolerance.
        result = reconcile_settlement(txns, _settlement(97_690), items, rules)
        assert result.all_exceptions() == []

    def test_no_transaction_is_silently_dropped(self, rules: Rules) -> None:
        """SRS 4.3.2: every input row appears as verified or as an exception."""
        txns = pd.DataFrame(
            [
                _txn("pay_ok", 100_000),
                _txn("pay_bad_fee", 50_000),
                _txn("pay_orphan", 20_000),
                _txn("pay_pending", 30_000, status="pending"),
            ]
        )
        items = pd.DataFrame(
            [_line_item("pay_ok", 2_000, 360), _line_item("pay_bad_fee", 9_999, 180)]
        )
        result = reconcile_settlement(txns, _settlement(97_640), items, rules)

        assert result.transactions_processed == 4
        assert result.verified_count + result.exception_count == 4
        covered = {o.transaction_id for o in result.outcomes}
        assert covered == {"pay_ok", "pay_bad_fee", "pay_orphan", "pay_pending"}

    def test_empty_batch_reports_zero_accuracy_not_perfect(self, rules: Rules) -> None:
        """An empty batch must never render as a 100% all-clear (PRD 3.2)."""
        result = reconcile_settlement(
            pd.DataFrame(columns=["transaction_id", "amount_paise", "method",
                                  "status", "settlement_id"]),
            _settlement(0),
            pd.DataFrame(),
            rules,
        )
        assert result.accuracy == 0.0
        assert result.transactions_processed == 0
