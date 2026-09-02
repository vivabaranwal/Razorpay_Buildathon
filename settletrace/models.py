"""Persistence model for SettleTrace (SRS section 5.1).

Every monetary field is an integer count of paise, Razorpay's minor unit. Money
is never held as a float here: comparing settlement totals is the whole point of
this system, and binary floating point cannot represent decimal currency exactly,
so a float model would manufacture penny-sized exceptions that are artefacts of
the representation rather than real merchant discrepancies.

The schema is deliberately forward-compatible with the V2 append-only ledger in
the PRD roadmap: correction history lives in its own table rather than being
overwritten in place.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MatchStatus(str, enum.Enum):
    VERIFIED = "verified"
    EXCEPTION = "exception"


class ReasonCode(str, enum.Enum):
    """Exception classifications (FR-1.5).

    FR-1.5 requires a specific reason code rather than a generic failure, so a
    reviewer knows what kind of problem they are looking at before opening it.
    """

    UNMATCHED_TRANSACTION = "unmatched_transaction"
    AMOUNT_MISMATCH = "amount_mismatch"
    FEE_MISMATCH = "fee_mismatch"
    GST_MISMATCH = "gst_mismatch"
    RESERVE_MISMATCH = "reserve_mismatch"
    LATE_AUTHORIZATION_PENDING = "late_authorization_pending"
    SETTLEMENT_TOTAL_MISMATCH = "settlement_total_mismatch"


class ExplanationSource(str, enum.Enum):
    """Where an exception's explanation text came from.

    Tracked as data rather than inferred from the text, because the UI must
    state which one the reviewer is reading. A fallback template presented as
    an AI explanation would be a claim the system cannot support.
    """

    LLM = "llm"
    FALLBACK = "fallback"


class PaymentStatus(str, enum.Enum):
    """Razorpay payment lifecycle states.

    CREATED, PENDING and AUTHORIZED are non-terminal: an order sitting in one of
    these past its resolution window is a stuck-candidate (FR-2.1).
    """

    CREATED = "created"
    PENDING = "pending"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    REFUNDED = "refunded"
    FAILED = "failed"

    @classmethod
    def non_terminal(cls) -> set["PaymentStatus"]:
        return {cls.CREATED, cls.PENDING, cls.AUTHORIZED}

    @property
    def is_terminal(self) -> bool:
        return self not in self.non_terminal()


class Batch(Base):
    """One run of the reconciliation engine (FRS glossary)."""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    settlement_id: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    transactions_processed: Mapped[int] = mapped_column(Integer, default=0)
    transactions_verified: Mapped[int] = mapped_column(Integer, default=0)
    transactions_exception: Mapped[int] = mapped_column(Integer, default=0)
    accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Set when the batch ran against a labelled sample whose correct answer is
    # known. FR-1.6 forbids quoting accuracy as a measured result otherwise.
    is_labeled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Detection quality against the labelled ground truth. Null on an unlabelled
    # batch, where there is no correct answer to measure against. A single
    # accuracy percentage cannot distinguish "found every defect" from "flagged
    # everything indiscriminately", which is why FR-1.6 requires both.
    precision: Mapped[float | None] = mapped_column(Float, nullable=True)
    recall: Mapped[float | None] = mapped_column(Float, nullable=True)
    true_positives: Mapped[int | None] = mapped_column(Integer, nullable=True)
    false_positives: Mapped[int | None] = mapped_column(Integer, nullable=True)
    false_negatives: Mapped[int | None] = mapped_column(Integer, nullable=True)

    matches: Mapped[list["Match"]] = relationship(back_populates="batch")


class Transaction(Base):
    __tablename__ = "transactions"

    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    method: Mapped[str] = mapped_column(String(32))
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Razorpay's own attribution of this payment to a settlement. Null until the
    # payment is settled.
    settlement_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True
    )


class Settlement(Base):
    __tablename__ = "settlements"

    settlement_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    utr: Mapped[str] = mapped_column(String(64), index=True)
    amount_paise: Mapped[int] = mapped_column(Integer)
    fees_paise: Mapped[int] = mapped_column(Integer, default=0)
    tax_paise: Mapped[int] = mapped_column(Integer, default=0)
    settled_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="ingested")
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    line_items: Mapped[list["SettlementLineItem"]] = relationship(
        back_populates="settlement"
    )


class SettlementLineItem(Base):
    """What Razorpay actually deducted for one transaction in a settlement.

    This is the *actual* side of the FR-1.4 comparison; the expected side is
    recomputed independently by the fee verifier from the merchant's rate card.
    """

    __tablename__ = "settlement_line_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    settlement_id: Mapped[str] = mapped_column(
        ForeignKey("settlements.settlement_id"), index=True
    )
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.transaction_id"), index=True
    )
    fee_paise: Mapped[int] = mapped_column(Integer, default=0)
    gst_paise: Mapped[int] = mapped_column(Integer, default=0)
    reserve_paise: Mapped[int] = mapped_column(Integer, default=0)

    settlement: Mapped[Settlement] = relationship(back_populates="line_items")

    __table_args__ = (
        UniqueConstraint("settlement_id", "transaction_id", name="uq_line_item"),
    )


class Match(Base):
    """Outcome of reconciling one transaction against a settlement (FR-1.3)."""

    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    transaction_id: Mapped[str] = mapped_column(String(64), index=True)
    settlement_id: Mapped[str] = mapped_column(String(64), index=True)
    match_status: Mapped[MatchStatus] = mapped_column(Enum(MatchStatus))

    expected_net_paise: Mapped[int] = mapped_column(Integer, default=0)
    actual_net_paise: Mapped[int] = mapped_column(Integer, default=0)

    batch: Mapped[Batch] = relationship(back_populates="matches")
    exception: Mapped["Exception_"] = relationship(
        back_populates="match", uselist=False
    )


class Exception_(Base):
    """A record the system could not verify with confidence (FR-1.5).

    Named with a trailing underscore only to avoid shadowing the builtin; the
    table and the API both call it ``exception``.
    """

    __tablename__ = "exceptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)

    reason_code: Mapped[ReasonCode] = mapped_column(Enum(ReasonCode), index=True)
    transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    settlement_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The signed difference that triggered this exception, in paise. Positive
    # means the merchant was deducted more than expected.
    delta_paise: Mapped[int] = mapped_column(Integer, default=0)
    expected_paise: Mapped[int] = mapped_column(Integer, default=0)
    actual_paise: Mapped[int] = mapped_column(Integer, default=0)

    # Written by the LLM layer (FR-3.1). Advisory text only: FR-3.1's business
    # rule forbids the LLM from touching any other column on this row.
    explanation_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation_source: Mapped[ExplanationSource | None] = mapped_column(
        Enum(ExplanationSource), nullable=True
    )
    impact_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impact_score: Mapped[int] = mapped_column(Integer, default=0)

    resolved_flag: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    match: Mapped[Match] = relationship(back_populates="exception")


class Order(Base):
    """Merchant-side order record - the thing that goes stale (Module 2)."""

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_paise: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(32))

    # The merchant's local belief about the payment, which may be wrong.
    local_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    is_stuck_candidate: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True
    )
    recheck_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_recheck_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )


class OrderStatusCheck(Base):
    """Audit trail for Module 2 (FR-2.4).

    Append-only. FR-2.4's business rule forbids silent overwrites, so every
    poll of Razorpay writes a row here whether or not it changed anything.
    """

    __tablename__ = "order_status_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    expected_status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus))
    actual_status_from_api: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus))
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    corrected_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=0)


class WebhookEvent(Base):
    """Seen webhook event IDs, for idempotent processing (FR-2.5).

    The primary key is Razorpay's ``x-razorpay-event-id``: a duplicate delivery
    collides on insert, which is what makes the deduplication atomic rather than
    a check-then-act race between two concurrent deliveries.
    """

    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AuditLog(Base):
    """Append-only record of every state change the system makes.

    The table is the answer to "why does this record say what it says". Nothing
    in SettleTrace may change a stored value without writing a row here, and
    nothing ever updates or deletes a row once written - an audit trail that can
    be edited is not an audit trail. ``changed_by`` distinguishes an automatic
    correction from a named human action, because the two carry very different
    weight when a merchant is reconstructing what happened to a payout.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    field_changed: Mapped[str] = mapped_column(String(64))

    # Stored as text rather than typed columns: this table spans order statuses,
    # boolean resolution flags and anything added later, and a rendered string is
    # what an auditor reads back anyway.
    old_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(255), nullable=True)

    changed_by: Mapped[str] = mapped_column(String(128), default="system")
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


Index("ix_exceptions_open", Exception_.batch_id, Exception_.resolved_flag)
Index("ix_audit_entity", AuditLog.entity_type, AuditLog.entity_id)
