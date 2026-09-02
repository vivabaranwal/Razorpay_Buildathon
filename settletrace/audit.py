"""The append-only audit trail.

Every state change SettleTrace makes goes through :func:`record_change`. The
table is the answer to "why does this record say what it says" - and an audit
trail is only evidence if nothing can rewrite it, so this module deliberately
offers no update and no delete. Reads are served by :func:`read_trail`.

``record_change`` adds its row to the caller's session rather than committing
it. That is what lets a correction and its audit row land in one transaction:
FR-2.4 forbids a status change without a trail entry, and a self-committing
audit helper would allow exactly that gap to open if the caller's own commit
later failed.
"""

from __future__ import annotations

import enum
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .models import AuditLog

logger = logging.getLogger(__name__)

# Entity type constants. Named rather than inlined so a typo becomes an import
# error instead of a row that silently never matches a filter.
ENTITY_ORDER = "order"
ENTITY_EXCEPTION = "exception"
ENTITY_SETTLEMENT_MATCH = "settlement_match"

SYSTEM_ACTOR = "system"

# Values are stored as text; anything longer than the column is truncated with a
# marker rather than raising, because losing the audit row would be worse than
# losing the tail of one long value.
_MAX_VALUE_LEN = 255


def render_value(value: Any) -> str | None:
    """Render any stored value as the text an auditor reads back.

    Enums render as their value rather than ``PaymentStatus.CAPTURED``, and
    booleans as ``true``/``false``, so the trail reads consistently regardless of
    which Python type the field happened to hold.
    """
    if value is None:
        return None
    if isinstance(value, enum.Enum):
        text = str(value.value)
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)

    if len(text) > _MAX_VALUE_LEN:
        return text[: _MAX_VALUE_LEN - 1] + "…"
    return text


def record_change(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    field_changed: str,
    old_value: Any,
    new_value: Any,
    reason: str | None = None,
    changed_by: str = SYSTEM_ACTOR,
    changed_at: datetime | None = None,
) -> AuditLog:
    """Append one row to the trail, in the caller's transaction.

    Returns the row so a caller can assert on it in a test, but the row is the
    point - not the return value.
    """
    entry = AuditLog(
        entity_type=entity_type,
        entity_id=str(entity_id),
        field_changed=field_changed,
        old_value=render_value(old_value),
        new_value=render_value(new_value),
        changed_by=changed_by,
        reason=reason,
    )
    if changed_at is not None:
        entry.changed_at = changed_at

    session.add(entry)
    logger.debug(
        "audit: %s %s %s %r -> %r by %s",
        entity_type,
        entity_id,
        field_changed,
        entry.old_value,
        entry.new_value,
        changed_by,
    )
    return entry


def read_trail(
    session: Session,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 200,
) -> list[AuditLog]:
    """Return the trail newest first, optionally narrowed to one entity.

    Ordered by id rather than by ``changed_at``: several rows written inside one
    transaction can share a timestamp to the microsecond, and the insertion
    order is the true sequence of events.
    """
    stmt = select(AuditLog)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == str(entity_id))

    stmt = stmt.order_by(desc(AuditLog.id)).limit(limit)
    return list(session.scalars(stmt).all())
