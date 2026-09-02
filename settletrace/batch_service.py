"""Batch orchestration: run a settlement reconciliation end to end (UC-1).

Sequencing matters here and is fixed by FR-3.1: the deterministic result is
computed and persisted *first*, and only then is the LLM asked for explanatory
text. An explanation can therefore never influence an outcome - if the LLM is
slow, wrong, or unreachable, the exception list is already written and correct.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import ENTITY_EXCEPTION, record_change
from .config import Rules, Settings
from .engine import evaluate_detection, reconcile_settlement
from .explainer import ExceptionSummary, Explainer, compute_impact_score, rank_exceptions
from .models import (
    Batch,
    Exception_,
    ExplanationSource,
    Match,
    MatchStatus,
    PaymentStatus,
    ReasonCode,
    Settlement,
    SettlementLineItem,
    Transaction,
    utcnow,
)
from .providers.base import DataProvider

logger = logging.getLogger(__name__)

# How far either side of the settlement date to look for transactions. A payout
# covers a preceding capture window, so the range is asymmetric - mostly before.
WINDOW_BEFORE = timedelta(days=2)
WINDOW_AFTER = timedelta(hours=12)


def run_settlement_batch(
    session: Session,
    settlement_id: str,
    provider: DataProvider,
    rules: Rules,
    settings: Settings,
    explain: bool = True,
    is_labeled: bool = False,
) -> Batch:
    """Ingest, reconcile, persist, then explain (FR-1.1 to FR-1.6, FR-3.x)."""
    batch = Batch(settlement_id=settlement_id, is_labeled=is_labeled)
    session.add(batch)
    session.flush()

    settlement = _ingest_settlement(session, settlement_id, provider)
    transactions = _ingest_transactions(session, settlement, provider)
    line_items = _ingest_line_items(session, settlement_id, provider)

    result = reconcile_settlement(
        transactions=transactions,
        settlement={
            "settlement_id": settlement.settlement_id,
            "amount_paise": settlement.amount_paise,
        },
        line_items=line_items,
        rules=rules,
    )

    # Persist the deterministic outcome before any LLM call (FR-3.1).
    amounts = _amount_lookup(transactions)
    methods = _method_lookup(transactions)
    exception_rows = _persist_result(session, batch, result, amounts, methods)

    batch.transactions_processed = result.transactions_processed
    batch.transactions_verified = result.verified_count
    batch.transactions_exception = result.exception_count
    batch.accuracy = result.accuracy

    # FR-1.6: on a labelled batch the planted defects are known, so detection
    # quality is measurable rather than merely asserted. An unlabelled batch
    # leaves these null - there is no ground truth to score against, and a
    # fabricated precision figure would be worse than an absent one.
    if is_labeled:
        _record_detection_metrics(batch, result, provider, settlement_id)

    batch.completed_at = utcnow()
    session.flush()

    if explain and exception_rows:
        _attach_explanations(session, exception_rows, amounts, methods, settings)

    logger.info(
        "Batch %d complete: %d processed, %.1f%% verified, %d exceptions",
        batch.id,
        batch.transactions_processed,
        (batch.accuracy or 0) * 100,
        batch.transactions_exception,
    )
    return batch


def _nullable(value):
    """Normalise pandas' missing-value sentinels back to ``None``.

    A DataFrame column holding datetimes converts ``None`` to ``NaT``, and a
    numeric column converts it to ``NaN``. Neither is storable, and NaT reaches
    the driver as a datetime-like object that fails deep inside the dialect
    rather than at the call site. Every optional field crossing the DataFrame
    boundary passes through here.
    """
    return None if value is None or pd.isna(value) else value


def _ingest_settlement(
    session: Session, settlement_id: str, provider: DataProvider
) -> Settlement:
    """FR-1.1. A repeat ingestion updates rather than duplicates the record."""
    raw = provider.fetch_settlement(settlement_id)

    settlement = session.get(Settlement, settlement_id)
    if settlement is None:
        settlement = Settlement(settlement_id=settlement_id)
        session.add(settlement)

    settlement.utr = raw.get("utr", "")
    settlement.amount_paise = int(raw["amount_paise"])
    settlement.fees_paise = int(raw.get("fees_paise", 0))
    settlement.tax_paise = int(raw.get("tax_paise", 0))
    settlement.settled_at = raw["settled_at"]
    settlement.status = "ingested"
    session.flush()
    return settlement


def _ingest_transactions(
    session: Session, settlement: Settlement, provider: DataProvider
) -> pd.DataFrame:
    """FR-1.2. Fetch the covering window and upsert each transaction."""
    settled_at = settlement.settled_at
    frame = provider.fetch_transactions(
        settled_at - WINDOW_BEFORE, settled_at + WINDOW_AFTER
    )
    if frame.empty:
        return frame

    for row in frame.to_dict("records"):
        txn = session.get(Transaction, row["transaction_id"])
        if txn is None:
            txn = Transaction(transaction_id=row["transaction_id"])
            session.add(txn)
        txn.order_id = row.get("order_id") or ""
        txn.amount_paise = int(row["amount_paise"])
        txn.currency = row.get("currency", "INR")
        txn.method = row["method"]
        txn.status = PaymentStatus(row["status"])
        txn.created_at = row["created_at"]
        txn.captured_at = _nullable(row.get("captured_at"))
        txn.settlement_id = _nullable(row.get("settlement_id"))

    session.flush()
    return frame


def _ingest_line_items(
    session: Session, settlement_id: str, provider: DataProvider
) -> pd.DataFrame:
    frame = provider.fetch_line_items(settlement_id)
    if frame.empty:
        return frame

    existing = {
        (li.settlement_id, li.transaction_id): li
        for li in session.scalars(
            select(SettlementLineItem).where(
                SettlementLineItem.settlement_id == settlement_id
            )
        )
    }

    for row in frame.to_dict("records"):
        key = (settlement_id, row["transaction_id"])
        item = existing.get(key)
        if item is None:
            item = SettlementLineItem(
                settlement_id=settlement_id, transaction_id=row["transaction_id"]
            )
            session.add(item)
        item.fee_paise = int(row.get("fee_paise", 0))
        item.gst_paise = int(row.get("gst_paise", 0))
        item.reserve_paise = int(row.get("reserve_paise", 0))

    session.flush()
    return frame


def _amount_lookup(transactions: pd.DataFrame) -> dict[str, int]:
    if transactions.empty:
        return {}
    return {
        str(r["transaction_id"]): int(r["amount_paise"])
        for r in transactions.to_dict("records")
    }


def _method_lookup(transactions: pd.DataFrame) -> dict[str, str]:
    if transactions.empty:
        return {}
    return {
        str(r["transaction_id"]): str(r["method"])
        for r in transactions.to_dict("records")
    }


def _persist_result(
    session: Session,
    batch: Batch,
    result,
    amounts: dict[str, int],
    methods: dict[str, str],
) -> list[Exception_]:
    """Write matches and exceptions, then rank them (FR-3.2)."""
    exception_rows: list[Exception_] = []

    for outcome in result.outcomes:
        match = Match(
            batch_id=batch.id,
            transaction_id=outcome.transaction_id,
            settlement_id=outcome.settlement_id,
            match_status=outcome.match_status,
            expected_net_paise=outcome.expected_net_paise,
            actual_net_paise=outcome.actual_net_paise,
        )
        session.add(match)
        session.flush()

        for exc in outcome.exceptions:
            exception_rows.append(
                _new_exception(session, batch, match.id, exc, amounts, methods)
            )

    # The settlement-level exception belongs to the batch, not to any one
    # transaction, so it gets a match row of its own to hang from.
    if result.settlement_total_exception:
        placeholder = Match(
            batch_id=batch.id,
            transaction_id="",
            settlement_id=result.settlement_id,
            match_status=MatchStatus.EXCEPTION,
        )
        session.add(placeholder)
        session.flush()
        exception_rows.append(
            _new_exception(
                session,
                batch,
                placeholder.id,
                result.settlement_total_exception,
                amounts,
                methods,
            )
        )

    session.flush()
    _apply_ranks(exception_rows, amounts, methods)
    session.flush()
    return exception_rows


def _new_exception(
    session: Session,
    batch: Batch,
    match_id: int,
    exc: dict,
    amounts: dict[str, int],
    methods: dict[str, str],
) -> Exception_:
    txn_id = exc.get("transaction_id")
    row = Exception_(
        match_id=match_id,
        batch_id=batch.id,
        reason_code=exc["reason_code"],
        transaction_id=txn_id,
        settlement_id=exc.get("settlement_id"),
        expected_paise=exc["expected_paise"],
        actual_paise=exc["actual_paise"],
        delta_paise=exc["delta_paise"],
    )
    row.impact_score = compute_impact_score(
        _summarize(row, amounts, methods)
    )
    session.add(row)
    return row


def _summarize(
    row: Exception_, amounts: dict[str, int], methods: dict[str, str]
) -> ExceptionSummary:
    txn_id = row.transaction_id or ""
    return ExceptionSummary(
        reason_code=row.reason_code,
        expected_paise=row.expected_paise,
        actual_paise=row.actual_paise,
        delta_paise=row.delta_paise,
        transaction_amount_paise=amounts.get(txn_id, abs(row.delta_paise)),
        method=methods.get(txn_id, "unknown"),
    )


def _apply_ranks(
    rows: list[Exception_], amounts: dict[str, int], methods: dict[str, str]
) -> None:
    if not rows:
        return
    summaries = [_summarize(r, amounts, methods) for r in rows]
    for row, rank in zip(rows, rank_exceptions(summaries)):
        row.impact_rank = rank


def _attach_explanations(
    session: Session,
    rows: list[Exception_],
    amounts: dict[str, int],
    methods: dict[str, str],
    settings: Settings,
) -> None:
    """FR-3.1: add explanatory text, touching no other field.

    Explanations are generated highest-impact first so that if the run is
    interrupted, the exceptions a reviewer opens first are the ones already
    explained.
    """
    explainer = Explainer(settings)
    for row in sorted(rows, key=lambda r: r.impact_rank or 0):
        result = explainer.explain(_summarize(row, amounts, methods))
        # Both fields are written from one result object so the text and its
        # provenance can never disagree. The UI labels the explanation from
        # ``explanation_source``; a fallback string shown under an "AI-generated"
        # label would be a claim the system cannot support.
        row.explanation_text = result.text
        row.explanation_source = result.source
    session.flush()


def _record_detection_metrics(
    batch: Batch, result, provider: DataProvider, settlement_id: str
) -> None:
    """Score the engine's output against the batch's known defects (FR-1.6).

    Only the sample provider knows what was planted. A provider that cannot
    answer leaves the metrics null rather than guessing, because a precision
    figure derived from the engine's own output would measure nothing.
    """
    labels = getattr(provider, "injected_defects_for", None)
    if labels is None:
        return

    injected = labels(settlement_id)
    if injected is None:
        return

    detected = [
        (exc.get("transaction_id"), exc["reason_code"])
        for exc in result.all_exceptions()
    ]
    metrics = evaluate_detection(detected, injected)

    batch.precision = metrics.precision
    batch.recall = metrics.recall
    batch.true_positives = metrics.true_positives
    batch.false_positives = metrics.false_positives
    batch.false_negatives = metrics.false_negatives

    logger.info(
        "Detection quality: precision %.3f, recall %.3f (tp=%d fp=%d fn=%d)",
        metrics.precision,
        metrics.recall,
        metrics.true_positives,
        metrics.false_positives,
        metrics.false_negatives,
    )


def resolve_exception(
    session: Session, exception_id: int, resolved_by: str = "merchant_user"
) -> Exception_ | None:
    """Mark an exception resolved by a human reviewer (UC-2).

    Only a human closes an exception: FR-1.5's business rule forbids the system
    discarding one automatically. The reviewer's name is required rather than
    defaulted at the API boundary, because "who signed this off" is the question
    the audit trail exists to answer.
    """
    row = session.get(Exception_, exception_id)
    if row is None:
        return None

    was_resolved = row.resolved_flag
    row.resolved_flag = True
    row.resolved_at = utcnow()
    row.resolved_by = resolved_by

    if not was_resolved:
        record_change(
            session,
            entity_type=ENTITY_EXCEPTION,
            entity_id=str(row.id),
            field_changed="resolved_flag",
            old_value=False,
            new_value=True,
            reason=(
                f"Reviewed and closed by {resolved_by}: "
                f"{row.reason_code.value} on "
                f"{row.transaction_id or row.settlement_id or 'settlement total'}"
            ),
            changed_by=resolved_by,
        )

    session.flush()
    return row
