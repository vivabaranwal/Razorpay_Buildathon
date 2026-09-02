"""Batch report export (CSV and JSON).

The exported file is the artifact a merchant attaches to an email when querying
a deduction with Razorpay, and the one month-end close opens in a spreadsheet.
It therefore has to stand on its own: amounts in rupees as well as paise, the
data provenance stated in the file itself, and the explanation source recorded
per row so a reader can tell model-written prose from a template.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Batch, Exception_

EXPORT_COLUMNS = [
    "impact_rank",
    "reason_code",
    "transaction_id",
    "settlement_id",
    "expected_inr",
    "actual_inr",
    "delta_inr",
    "value_at_risk_inr",
    "explanation",
    "explanation_source",
    "resolved",
    "resolved_by",
    "resolved_at",
]


def _rupees(paise: int | None) -> str:
    """Render paise as a plain decimal string.

    No thousands separators and no currency symbol: the file is meant to be
    opened in a spreadsheet, and a formatted string would import as text rather
    than as a number you can sum.
    """
    if paise is None:
        return ""
    return f"{paise / 100:.2f}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _exceptions_for(session: Session, batch: Batch) -> list[Exception_]:
    """Every exception in the batch, resolved ones included.

    The export deliberately ignores the dashboard's open-only default: a report
    that omitted resolved items would misrepresent what the batch found.
    """
    stmt = (
        select(Exception_)
        .where(Exception_.batch_id == batch.id)
        .order_by(Exception_.impact_rank.is_(None), Exception_.impact_rank)
    )
    return list(session.scalars(stmt).all())


def _summary_dict(batch: Batch, exceptions: list[Exception_]) -> dict:
    open_rows = [e for e in exceptions if not e.resolved_flag]
    return {
        "batch_id": batch.id,
        "settlement_id": batch.settlement_id,
        "started_at": _iso(batch.started_at),
        "completed_at": _iso(batch.completed_at),
        "transactions_processed": batch.transactions_processed,
        "transactions_verified": batch.transactions_verified,
        "transactions_exception": batch.transactions_exception,
        "accuracy_pct": round(batch.accuracy * 100, 2)
        if batch.accuracy is not None
        else None,
        # Whether that accuracy figure may be quoted as a measured result. A
        # report that omitted this would let a throughput number from unlabelled
        # data be read as a verified accuracy claim.
        "accuracy_is_measured": batch.is_labeled,
        "data_provenance": (
            "Labelled sample batch with injected defects - accuracy is measured "
            "against known ground truth"
            if batch.is_labeled
            else "Unlabelled data - the match rate is throughput, not a measured "
            "accuracy result"
        ),
        "open_exceptions": len(open_rows),
        "value_at_risk_inr": _rupees(sum(e.impact_score for e in open_rows)),
        "exported_at": datetime.now().isoformat(),
    }


def _exception_row(exc: Exception_) -> dict:
    return {
        "impact_rank": exc.impact_rank,
        "reason_code": exc.reason_code.value,
        "transaction_id": exc.transaction_id or "",
        "settlement_id": exc.settlement_id or "",
        "expected_inr": _rupees(exc.expected_paise),
        "actual_inr": _rupees(exc.actual_paise),
        "delta_inr": _rupees(exc.delta_paise),
        "value_at_risk_inr": _rupees(exc.impact_score),
        "explanation": exc.explanation_text or "",
        "explanation_source": (
            exc.explanation_source.value if exc.explanation_source else "none"
        ),
        "resolved": "yes" if exc.resolved_flag else "no",
        "resolved_by": exc.resolved_by or "",
        "resolved_at": _iso(exc.resolved_at) or "",
    }


def export_json(session: Session, batch: Batch) -> str:
    """The batch as JSON: summary object plus the full exception list."""
    exceptions = _exceptions_for(session, batch)
    return json.dumps(
        {
            "summary": _summary_dict(batch, exceptions),
            "exceptions": [_exception_row(e) for e in exceptions],
        },
        indent=2,
    )


def export_csv(session: Session, batch: Batch) -> str:
    """The batch as CSV, with the summary as leading comment lines.

    The summary rides in ``#``-prefixed lines above the header so the file is
    still a single valid CSV that opens cleanly, while the provenance note
    travels with the data rather than being lost on download.
    """
    exceptions = _exceptions_for(session, batch)
    summary = _summary_dict(batch, exceptions)

    buffer = io.StringIO()
    for key, value in summary.items():
        buffer.write(f"# {key}: {value}\n")
    buffer.write("#\n")

    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for exc in exceptions:
        writer.writerow(_exception_row(exc))

    return buffer.getvalue()


def export_filename(batch: Batch, format: str) -> str:
    """A filename that identifies the batch without needing the file opened."""
    stamp = (batch.completed_at or batch.started_at or datetime.now()).strftime(
        "%Y%m%d-%H%M"
    )
    return f"settletrace-batch{batch.id}-{batch.settlement_id}-{stamp}.{format}"
