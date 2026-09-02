"""The provider interface shared by the sandbox client and the sample generator."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import pandas as pd


class DataProvider(Protocol):
    """Source of ground-truth payment and settlement data."""

    def fetch_settlement(self, settlement_id: str) -> dict:
        """Settlement metadata (FR-1.1): id, utr, amounts, settled_at."""
        ...

    def fetch_transactions(
        self, start: datetime, end: datetime
    ) -> pd.DataFrame:
        """Every transaction in the settlement window (FR-1.2), paginated."""
        ...

    def fetch_line_items(self, settlement_id: str) -> pd.DataFrame:
        """Razorpay's reported per-transaction deductions for a settlement."""
        ...

    def fetch_payment_status(self, payment_id: str) -> str:
        """Ground-truth status for one payment (FR-2.3)."""
        ...
