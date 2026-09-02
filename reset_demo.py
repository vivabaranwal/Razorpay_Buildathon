"""Wipe and reseed the database to the exact known-good demo state.

For recording: run this between takes and every run produces an identical
starting point - same settlement ID, same 250 transactions, same exceptions,
same three stuck orders, no audit history, nothing resolved.

    python reset_demo.py

The sample generator is seeded, so the batch is reproducible. The stuck orders
are the one moving part - their timestamps are relative to now, so they are
always freshly past their window and ready for the scheduler to correct on
camera rather than having been corrected during a previous take.

Safe to run with the backend up: it drops and recreates the tables rather than
deleting the file, so a running server's connection pool stays valid.
"""

from __future__ import annotations

import argparse
import logging
import sys

from settletrace.batch_service import run_settlement_batch
from settletrace.config import get_rules, get_settings
from settletrace.db import engine, init_db, session_scope
from settletrace.models import Base, Order, PaymentStatus
from settletrace.providers import get_provider
from settletrace.providers.factory import reset_provider
from settletrace.providers.sample import SampleDataProvider

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

# The fixed demo scenario. Changing these changes what the recording shows, so
# they live here as named constants rather than scattered as literals.
SETTLEMENT_ID = "setl_DEMO001"
TRANSACTIONS = 250
DEFECT_RATE = 0.04
STUCK_ORDERS = 3
HEALTHY_ORDERS = 12


def reset() -> dict:
    """Drop every table, recreate, and reseed. Returns the resulting counts."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    init_db()

    # A fresh provider, so a generator that ran earlier in this process cannot
    # leak its state into the reseeded batch.
    reset_provider()
    rules = get_rules()
    settings = get_settings()

    # The demo batch is always generated, never fetched, so a reset produces
    # the same 250 rows whether or not sandbox credentials are configured.
    provider = SampleDataProvider(rules)
    sample = provider.generate(
        SETTLEMENT_ID, n_transactions=TRANSACTIONS, defect_rate=DEFECT_RATE
    )

    with session_scope() as session:
        batch = run_settlement_batch(
            session=session,
            settlement_id=SETTLEMENT_ID,
            provider=provider,
            rules=rules,
            settings=settings,
            explain=True,
            is_labeled=True,
        )
        result = {
            "batch_id": batch.id,
            "processed": batch.transactions_processed,
            "verified": batch.transactions_verified,
            "exceptions": batch.transactions_exception,
            "accuracy_pct": round((batch.accuracy or 0) * 100, 1),
            "precision_pct": round((batch.precision or 0) * 100, 1),
            "recall_pct": round((batch.recall or 0) * 100, 1),
            "planted_defects": len(sample.injected_defects),
        }

    orders = provider.generate_stuck_orders(
        n_stuck=STUCK_ORDERS, n_healthy=HEALTHY_ORDERS
    )
    with session_scope() as session:
        for row in orders.to_dict("records"):
            session.add(
                Order(
                    order_id=row["order_id"],
                    payment_id=row["payment_id"],
                    amount_paise=int(row["amount_paise"]),
                    method=row["method"],
                    local_status=PaymentStatus(row["local_status"]),
                    created_at=row["created_at"],
                    updated_at=row["created_at"],
                )
            )

    # Point the running server's provider at the one holding this batch's
    # ground truth, so a re-check during the demo resolves correctly.
    import settletrace.providers.factory as factory

    factory._provider = provider

    result["orders"] = len(orders)
    result["stuck"] = STUCK_ORDERS
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet", action="store_true", help="print only the summary line"
    )
    args = parser.parse_args()

    result = reset()

    if args.quiet:
        print(
            f"reset: batch #{result['batch_id']}, {result['processed']} txns, "
            f"{result['exceptions']} exceptions, {result['stuck']} stuck orders"
        )
        return 0

    print()
    print("  Demo state reset")
    print("  " + "-" * 54)
    print(f"  Settlement ........ {SETTLEMENT_ID}")
    print(f"  Batch ............. #{result['batch_id']}")
    print(
        f"  Transactions ...... {result['processed']} "
        f"({result['verified']} verified, {result['exceptions']} exceptions)"
    )
    print(
        f"  Detection ......... {result['accuracy_pct']}% matched, "
        f"precision {result['precision_pct']}%, recall {result['recall_pct']}%"
    )
    print(f"  Planted defects ... {result['planted_defects']}")
    print(
        f"  Orders ............ {result['orders']} "
        f"({result['stuck']} deliberately stuck)"
    )
    print("  Audit trail ....... empty")
    print()
    print("  Restart the backend so it picks up the reseeded state:")
    print("    uvicorn settletrace.api.app:app --reload")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
