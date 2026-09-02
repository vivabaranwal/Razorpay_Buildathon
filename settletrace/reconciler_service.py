"""Module 2 - the Payment State Reconciler (FR-2.1 to FR-2.5).

This is the fallback Razorpay's own webhook documentation recommends and which,
per PRD 2.2, almost no small merchant implements: when a webhook is missed,
delayed or duplicated, poll the Payments API for ground truth and correct the
local order record.

Two rules shape the design. FR-2.4 forbids silent overwrites, so every poll
writes an audit row whether or not it changed anything. FR-2.2 requires backoff
with a cap, so a genuinely stuck order is neither hammered nor forgotten.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .audit import ENTITY_ORDER, record_change
from .config import Rules
from .models import Order, OrderStatusCheck, PaymentStatus, WebhookEvent, utcnow
from .providers.base import DataProvider

logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime.

    SQLite drops timezone information on write, so timestamps come back naive
    and comparing one against an aware ``now`` raises. Values are stored as UTC,
    so re-attaching it is a lossless repair rather than an assumption.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def detect_stuck_candidates(
    session: Session, rules: Rules, now: datetime | None = None
) -> list[Order]:
    """Flag orders non-terminal past their method's window (FR-2.1).

    The window is per method because normal resolution time genuinely differs -
    a UPI collect request that has not resolved in five minutes is suspicious,
    while a netbanking redirect at the same age is entirely ordinary.
    """
    now = now or utcnow()
    non_terminal = list(PaymentStatus.non_terminal())

    open_orders = session.scalars(
        select(Order).where(Order.local_status.in_(non_terminal))
    ).all()

    newly_stuck: list[Order] = []
    for order in open_orders:
        window = rules.resolution_window_for(order.method)
        elapsed = (now - _as_utc(order.created_at)).total_seconds()
        if elapsed <= window:
            continue

        if not order.is_stuck_candidate:
            order.is_stuck_candidate = True
            order.recheck_attempts = 0
            order.next_recheck_at = now  # first re-check is due immediately
            newly_stuck.append(order)

    if newly_stuck:
        logger.info("Flagged %d new stuck-candidate orders", len(newly_stuck))
    return newly_stuck


def due_for_recheck(
    session: Session, now: datetime | None = None
) -> list[Order]:
    """Stuck candidates whose next scheduled re-check has come due (FR-2.2)."""
    now = now or utcnow()
    candidates = session.scalars(
        select(Order).where(Order.is_stuck_candidate.is_(True))
    ).all()
    return [
        o
        for o in candidates
        if o.next_recheck_at is None or _as_utc(o.next_recheck_at) <= now
    ]


def recheck_order(
    session: Session,
    order: Order,
    provider: DataProvider,
    rules: Rules,
    now: datetime | None = None,
) -> OrderStatusCheck:
    """Poll Razorpay for one order and correct the local record (FR-2.3, FR-2.4).

    Returns the audit row. The row is written on every call, including the case
    where local and remote already agree, because 'we checked and it was fine'
    is itself part of the trail a merchant needs to trust the correction.
    """
    now = now or utcnow()

    if not order.payment_id:
        # No payment was ever attached, so there is nothing to poll. This is not
        # a desync; the order simply never reached a payment.
        raise ValueError(f"Order {order.order_id} has no payment_id to poll")

    raw_status = provider.fetch_payment_status(order.payment_id)
    try:
        true_status = PaymentStatus(raw_status)
    except ValueError:
        logger.warning(
            "Razorpay returned unrecognised status %r for %s; leaving local "
            "record untouched",
            raw_status,
            order.payment_id,
        )
        # An unknown status must not overwrite a known one - guessing here could
        # mark a live order terminal and stop it being re-checked.
        true_status = order.local_status

    previous = order.local_status
    corrected = true_status != previous

    check = OrderStatusCheck(
        order_id=order.order_id,
        expected_status=previous,
        actual_status_from_api=true_status,
        checked_at=now,
        corrected_flag=corrected,
        attempt_number=order.recheck_attempts,
    )
    session.add(check)

    if corrected:
        order.local_status = true_status
        order.updated_at = now
        # The correction and its audit row are added to the same session and
        # commit together. FR-2.4 forbids one without the other, so they are
        # written adjacently here rather than left to a caller to remember.
        record_change(
            session,
            entity_type=ENTITY_ORDER,
            entity_id=order.order_id,
            field_changed="local_status",
            old_value=previous,
            new_value=true_status,
            reason=(
                f"Razorpay ground-truth poll on payment {order.payment_id} "
                f"(re-check attempt {order.recheck_attempts + 1}) disagreed with "
                "the local record"
            ),
        )
        logger.info(
            "Corrected %s: %s -> %s", order.order_id, previous.value, true_status.value
        )

    if true_status.is_terminal:
        # Resolved one way or the other; stop re-checking it.
        order.is_stuck_candidate = False
        order.next_recheck_at = None
    else:
        _schedule_next_recheck(order, rules, now)

    return check


def _schedule_next_recheck(order: Order, rules: Rules, now: datetime) -> None:
    """Advance the backoff schedule, capping attempts (FR-2.2)."""
    order.recheck_attempts += 1

    if order.recheck_attempts >= rules.backoff_max_attempts:
        # The cap stops the polling, not the visibility: the order stays
        # non-terminal and keeps showing on the dashboard for a human, which is
        # what FR-2.2's business rule means by not deferring detection forever.
        order.is_stuck_candidate = False
        order.next_recheck_at = None
        logger.info(
            "Order %s exhausted %d re-checks and still unresolved; leaving for "
            "human review",
            order.order_id,
            rules.backoff_max_attempts,
        )
        return

    delay = rules.backoff_delay_for(order.recheck_attempts)
    order.next_recheck_at = now + timedelta(seconds=delay)


def run_reconciliation_cycle(
    session: Session,
    provider: DataProvider,
    rules: Rules,
    now: datetime | None = None,
) -> dict:
    """One full Module 2 pass: detect, then re-check everything due."""
    now = now or utcnow()

    newly_stuck = detect_stuck_candidates(session, rules, now)
    session.flush()

    checks = []
    for order in due_for_recheck(session, now):
        if not order.payment_id:
            continue
        checks.append(recheck_order(session, order, provider, rules, now))

    return {
        "newly_stuck": len(newly_stuck),
        "rechecked": len(checks),
        "corrected": sum(1 for c in checks if c.corrected_flag),
    }


def record_webhook_event(
    session: Session,
    event_id: str,
    event_type: str,
    payment_id: str | None = None,
) -> bool:
    """Record a webhook event, returning False if it is a duplicate (FR-2.5).

    Deduplication relies on the primary-key collision rather than a preceding
    SELECT. Two concurrent deliveries of the same event would both pass a
    check-then-act test and both be processed; letting the insert fail makes
    'first one wins' a property of the database rather than of timing.
    """
    event = WebhookEvent(
        event_id=event_id, event_type=event_type, payment_id=payment_id
    )
    try:
        with session.begin_nested():
            session.add(event)
        return True
    except IntegrityError:
        logger.info("Discarded duplicate webhook event %s", event_id)
        return False


def purge_expired_webhook_events(
    session: Session, rules: Rules, now: datetime | None = None
) -> int:
    """Drop event IDs older than Razorpay's retry window (FR-2.5 business rule).

    Retention must cover the documented 24-hour retry window; beyond that the
    IDs are dead weight, and an unbounded table would eventually dominate the
    database for no protective benefit.
    """
    now = now or utcnow()
    cutoff = now - timedelta(hours=rules.webhook_event_retention_hours)
    stale = session.scalars(
        select(WebhookEvent).where(WebhookEvent.received_at < cutoff)
    ).all()
    for event in stale:
        session.delete(event)
    return len(stale)
