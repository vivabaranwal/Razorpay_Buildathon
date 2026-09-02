"""Tests for Module 2, the Payment State Reconciler (FR-2.1 to FR-2.5).

These follow the acceptance criteria stated per requirement in the FRS.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from settletrace.config import Rules
from settletrace.models import Order, OrderStatusCheck, PaymentStatus, WebhookEvent
from settletrace.reconciler_service import (
    detect_stuck_candidates,
    due_for_recheck,
    purge_expired_webhook_events,
    recheck_order,
    record_webhook_event,
    run_reconciliation_cycle,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class FakeProvider:
    """Returns a scripted ground-truth status, and counts how often it is asked."""

    def __init__(self, truth: dict[str, str]) -> None:
        self.truth = truth
        self.calls: list[str] = []

    def fetch_payment_status(self, payment_id: str) -> str:
        self.calls.append(payment_id)
        return self.truth.get(payment_id, "pending")


def _order(
    session,
    order_id: str = "order_1",
    *,
    method: str = "upi",
    status: PaymentStatus = PaymentStatus.PENDING,
    age_seconds: int = 3600,
    payment_id: str | None = "pay_1",
) -> Order:
    order = Order(
        order_id=order_id,
        payment_id=payment_id,
        amount_paise=49_900,
        method=method,
        local_status=status,
        created_at=NOW - timedelta(seconds=age_seconds),
        updated_at=NOW - timedelta(seconds=age_seconds),
    )
    session.add(order)
    session.flush()
    return order


class TestStuckDetection:
    def test_order_past_window_is_flagged(self, session, rules: Rules) -> None:
        """FR-2.1 acceptance: a stale non-terminal order becomes a candidate."""
        _order(session, method="upi", age_seconds=3600)  # window is 300s
        stuck = detect_stuck_candidates(session, rules, NOW)

        assert len(stuck) == 1
        assert stuck[0].is_stuck_candidate is True

    def test_fresh_order_within_window_is_not_flagged(
        self, session, rules: Rules
    ) -> None:
        """FR-2.1 acceptance: an order inside its window is left alone."""
        _order(session, method="upi", age_seconds=60)  # window is 300s
        assert detect_stuck_candidates(session, rules, NOW) == []

    def test_window_is_per_payment_method(self, session, rules: Rules) -> None:
        """A netbanking redirect is slow by nature; UPI at the same age is not."""
        _order(session, "order_upi", method="upi", age_seconds=600)
        _order(session, "order_nb", method="netbanking", age_seconds=600)
        stuck = detect_stuck_candidates(session, rules, NOW)

        assert {o.order_id for o in stuck} == {"order_upi"}

    def test_captured_order_is_never_a_candidate(self, session, rules: Rules) -> None:
        _order(session, status=PaymentStatus.CAPTURED, age_seconds=99_999)
        assert detect_stuck_candidates(session, rules, NOW) == []

    def test_detection_is_idempotent(self, session, rules: Rules) -> None:
        """Re-running detection must not reset an in-progress backoff schedule."""
        _order(session, age_seconds=3600)
        detect_stuck_candidates(session, rules, NOW)
        assert detect_stuck_candidates(session, rules, NOW) == []


class TestGroundTruthPolling:
    def test_mismatch_is_detected_and_corrected(self, session, rules: Rules) -> None:
        """FR-2.3/FR-2.4 acceptance: Razorpay says captured, local says pending."""
        order = _order(session)
        order.is_stuck_candidate = True
        provider = FakeProvider({"pay_1": "captured"})

        check = recheck_order(session, order, provider, rules, NOW)

        assert check.corrected_flag is True
        assert check.expected_status is PaymentStatus.PENDING
        assert check.actual_status_from_api is PaymentStatus.CAPTURED
        assert order.local_status is PaymentStatus.CAPTURED

    def test_correction_writes_an_audit_row(self, session, rules: Rules) -> None:
        """FR-2.4 business rule: no silent overwrites."""
        order = _order(session)
        recheck_order(session, order, FakeProvider({"pay_1": "captured"}), rules, NOW)
        session.flush()

        rows = session.scalars(select(OrderStatusCheck)).all()
        assert len(rows) == 1
        assert rows[0].expected_status is PaymentStatus.PENDING
        assert rows[0].actual_status_from_api is PaymentStatus.CAPTURED

    def test_agreement_still_writes_an_audit_row(self, session, rules: Rules) -> None:
        """'We checked and it was fine' is part of the trail too."""
        order = _order(session)
        check = recheck_order(
            session, order, FakeProvider({"pay_1": "pending"}), rules, NOW
        )
        session.flush()

        assert check.corrected_flag is False
        assert len(session.scalars(select(OrderStatusCheck)).all()) == 1

    def test_unrecognised_status_does_not_overwrite(self, session, rules: Rules) -> None:
        """Guessing at an unknown status could wrongly mark an order terminal."""
        order = _order(session)
        recheck_order(
            session, order, FakeProvider({"pay_1": "some_new_state"}), rules, NOW
        )
        assert order.local_status is PaymentStatus.PENDING

    def test_resolved_order_stops_being_a_candidate(
        self, session, rules: Rules
    ) -> None:
        order = _order(session)
        order.is_stuck_candidate = True
        recheck_order(session, order, FakeProvider({"pay_1": "captured"}), rules, NOW)

        assert order.is_stuck_candidate is False
        assert order.next_recheck_at is None


class TestBackoff:
    def test_unresolved_order_backs_off_on_the_schedule(
        self, session, rules: Rules
    ) -> None:
        """FR-2.2 acceptance: re-checks occur at the configured intervals."""
        order = _order(session)
        order.is_stuck_candidate = True
        provider = FakeProvider({"pay_1": "pending"})

        recheck_order(session, order, provider, rules, NOW)
        # After attempt 0, the next delay is schedule[1] = 120s.
        assert order.recheck_attempts == 1
        assert order.next_recheck_at == NOW + timedelta(seconds=120)

        recheck_order(session, order, provider, rules, NOW)
        assert order.next_recheck_at == NOW + timedelta(seconds=600)

    def test_backoff_stops_at_the_cap(self, session, rules: Rules) -> None:
        """FR-2.2 business rule: the cap bounds polling, not visibility."""
        order = _order(session)
        order.is_stuck_candidate = True
        provider = FakeProvider({"pay_1": "pending"})

        for _ in range(rules.backoff_max_attempts):
            recheck_order(session, order, provider, rules, NOW)

        assert order.is_stuck_candidate is False
        # Still non-terminal, so it remains visible for a human reviewer.
        assert order.local_status is PaymentStatus.PENDING
        assert len(provider.calls) == rules.backoff_max_attempts

    def test_only_due_orders_are_rechecked(self, session, rules: Rules) -> None:
        order = _order(session)
        order.is_stuck_candidate = True
        order.next_recheck_at = NOW + timedelta(seconds=300)

        assert due_for_recheck(session, NOW) == []
        assert len(due_for_recheck(session, NOW + timedelta(seconds=301))) == 1


class TestWebhookDeduplication:
    def test_duplicate_delivery_is_processed_once(self, session) -> None:
        """FR-2.5 acceptance: the same event twice yields one update."""
        first = record_webhook_event(session, "evt_abc", "payment.captured", "pay_1")
        second = record_webhook_event(session, "evt_abc", "payment.captured", "pay_1")

        assert first is True
        assert second is False
        assert len(session.scalars(select(WebhookEvent)).all()) == 1

    def test_distinct_events_are_both_processed(self, session) -> None:
        assert record_webhook_event(session, "evt_1", "payment.captured") is True
        assert record_webhook_event(session, "evt_2", "payment.captured") is True

    def test_events_past_the_retry_window_are_purged(
        self, session, rules: Rules
    ) -> None:
        """FR-2.5 business rule: retain at least Razorpay's 24-hour window."""
        session.add(
            WebhookEvent(
                event_id="evt_old",
                event_type="payment.captured",
                received_at=NOW - timedelta(hours=30),
            )
        )
        session.add(
            WebhookEvent(
                event_id="evt_recent",
                event_type="payment.captured",
                received_at=NOW - timedelta(hours=2),
            )
        )
        session.flush()

        assert purge_expired_webhook_events(session, rules, NOW) == 1
        remaining = session.scalars(select(WebhookEvent)).all()
        assert [e.event_id for e in remaining] == ["evt_recent"]


class TestSampleProviderGroundTruth:
    def test_stuck_truth_survives_a_fresh_provider(self, rules: Rules) -> None:
        """Ground truth must not depend on generator state.

        The API server and the seeding script are separate processes, so a
        provider that only knew the truth for orders it had generated in-process
        reported every seeded stuck order as still pending - making a desync
        look like a payment that genuinely had not resolved.
        """
        from settletrace.providers.sample import SampleDataProvider

        # A provider that has never called generate_stuck_orders(), exactly as
        # the freshly started API server's provider has not.
        fresh = SampleDataProvider(rules)
        assert fresh.fetch_payment_status("pay_STUCK000") == "captured"

    def test_explicit_truth_still_wins(self, rules: Rules) -> None:
        from settletrace.providers.sample import SampleDataProvider

        provider = SampleDataProvider(rules)
        provider.generate_stuck_orders(n_stuck=1, n_healthy=0)
        assert provider.fetch_payment_status("pay_STUCK000") == "captured"


class TestFullCycle:
    def test_cycle_detects_and_corrects_in_one_pass(
        self, session, rules: Rules
    ) -> None:
        """The end-to-end Module 2 demo path from the FRS definition of done."""
        _order(session, "order_stuck", method="upi", age_seconds=3600,
               payment_id="pay_stuck")
        _order(session, "order_fresh", method="upi", age_seconds=30,
               payment_id="pay_fresh")

        provider = FakeProvider({"pay_stuck": "captured", "pay_fresh": "pending"})
        result = run_reconciliation_cycle(session, provider, rules, NOW)

        assert result["newly_stuck"] == 1
        assert result["rechecked"] == 1
        assert result["corrected"] == 1
        assert provider.calls == ["pay_stuck"]
