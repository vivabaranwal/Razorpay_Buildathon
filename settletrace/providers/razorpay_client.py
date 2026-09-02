"""Razorpay sandbox client (FR-1.2, FR-2.3).

Two behaviours the FRS calls out explicitly and that are easy to get wrong:

* Pagination (FR-1.2): the Payments API returns a page at a time, and a client
  that reads only the first page under-reports the window. Under-reporting here
  is worse than an outright failure - the missing transactions would look like a
  clean batch rather than an error.
* Rate limiting (FR-1.2, FR-2.3 business rules): a 429 is retried with backoff
  rather than failing the batch, and SRS 4.3.2 requires that a failed call not
  corrupt already-verified state, so failures raise rather than return partial
  data that a caller might mistake for a complete window.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import pandas as pd

from ..config import Settings

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0


class RazorpayUnavailable(RuntimeError):
    """Raised when Razorpay cannot be reached after exhausting retries."""


class RazorpayProvider:
    """Reads payments and settlements from Razorpay's API."""

    def __init__(self, settings: Settings) -> None:
        if not settings.razorpay_configured:
            raise ValueError(
                "Razorpay credentials are not configured. Set RAZORPAY_KEY_ID and "
                "RAZORPAY_KEY_SECRET, or run with SETTLETRACE_USE_SANDBOX=false to "
                "use generated sample data."
            )
        import razorpay  # imported lazily so sample-only runs need no SDK

        self._client = razorpay.Client(
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
        )

    def _call(self, fn, *args, **kwargs):
        """Invoke an SDK call, retrying transient failures with backoff.

        Backoff doubles per attempt. Credentials are never included in a log
        line here (SRS 4.3.3); only the operation name and attempt count are.
        """
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # SDK raises varied types per failure mode
                last_error = exc
                if not self._is_retryable(exc):
                    raise
                delay = BASE_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "Razorpay call failed (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)

        raise RazorpayUnavailable(
            f"Razorpay unavailable after {MAX_RETRIES} attempts"
        ) from last_error

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Only rate limits and server-side faults are worth retrying.

        A 401 or a malformed request will fail identically every time; retrying
        those just delays a clear error the operator needs to see.
        """
        text = str(exc).lower()
        return any(
            marker in text
            for marker in ("429", "rate limit", "timeout", "500", "502", "503", "504")
        )

    def fetch_settlement(self, settlement_id: str) -> dict:
        raw = self._call(self._client.settlement.fetch, settlement_id)
        return {
            "settlement_id": raw["id"],
            "utr": raw.get("utr", ""),
            "amount_paise": int(raw["amount"]),
            "fees_paise": int(raw.get("fees", 0)),
            "tax_paise": int(raw.get("tax", 0)),
            "settled_at": datetime.fromtimestamp(raw["created_at"]),
        }

    def fetch_transactions(self, start: datetime, end: datetime) -> pd.DataFrame:
        """Fetch every captured payment in the window, following pagination."""
        rows: list[dict] = []
        skip = 0

        while True:
            page = self._call(
                self._client.payment.all,
                {
                    "from": int(start.timestamp()),
                    "to": int(end.timestamp()),
                    "count": PAGE_SIZE,
                    "skip": skip,
                },
            )
            items = page.get("items", [])
            rows.extend(self._to_transaction_row(item) for item in items)

            # A short page means the window is exhausted. Checking the returned
            # count rather than a total field avoids an infinite loop if the API
            # reports a total that never agrees with what it actually serves.
            if len(items) < PAGE_SIZE:
                break
            skip += PAGE_SIZE

        logger.info("Fetched %d transactions for settlement window", len(rows))
        return pd.DataFrame(rows)

    @staticmethod
    def _to_transaction_row(item: dict) -> dict:
        captured_at = item.get("captured_at")
        return {
            "transaction_id": item["id"],
            "order_id": item.get("order_id") or "",
            "amount_paise": int(item["amount"]),
            "currency": item.get("currency", "INR"),
            "method": item.get("method", "unknown"),
            "status": item.get("status", "created"),
            "created_at": datetime.fromtimestamp(item["created_at"]),
            "captured_at": datetime.fromtimestamp(captured_at)
            if captured_at
            else None,
            # Razorpay does not return a settlement reference on the payment
            # object itself; it comes from the settlement recon report, which
            # fetch_line_items resolves.
            "settlement_id": None,
        }

    def fetch_line_items(self, settlement_id: str) -> pd.DataFrame:
        """Per-transaction deductions from the settlement recon report."""
        rows: list[dict] = []
        skip = 0

        while True:
            page = self._call(
                self._client.settlement.recon_entity,
                {"settlement_id": settlement_id, "count": PAGE_SIZE, "skip": skip},
            )
            items = page.get("items", [])
            for item in items:
                rows.append(
                    {
                        "transaction_id": item.get("entity_id", ""),
                        "settlement_id": settlement_id,
                        "fee_paise": int(item.get("fee", 0)),
                        "gst_paise": int(item.get("tax", 0)),
                        "reserve_paise": 0,
                    }
                )
            if len(items) < PAGE_SIZE:
                break
            skip += PAGE_SIZE

        return pd.DataFrame(rows)

    def fetch_payment_status(self, payment_id: str) -> str:
        """Ground-truth status for one payment (FR-2.3)."""
        raw = self._call(self._client.payment.fetch, payment_id)
        return raw.get("status", "created")
