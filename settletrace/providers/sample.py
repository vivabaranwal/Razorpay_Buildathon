"""Sample-data provider generating a labelled batch with injected defects.

FR-1.6's business rule is that accuracy may only be quoted as a measured result
against a batch whose correct answer is independently known. This generator
exists to produce exactly that: it plants a known number of defects of known
kinds, and records them in ``injected_defects`` so a test or the dashboard can
check the engine found precisely those and nothing else.

PRD section 10 also flags a risk here - sample data may not reflect the
diversity of real fee structures - and the mitigation is to label the data's
provenance rather than quote an unqualified accuracy number. Every batch built
from this provider is marked ``is_labeled`` so the UI can say where it came from.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..config import Rules
from ..engine.fees import compute_expected_fees
from ..models import ReasonCode

# Method mix roughly reflecting Razorpay's domestic volume, where UPI dominates.
METHOD_WEIGHTS = {
    "upi": 0.62,
    "card": 0.21,
    "netbanking": 0.11,
    "wallet": 0.06,
}


@dataclass
class InjectedDefect:
    """A defect deliberately planted so detection can be verified."""

    transaction_id: str
    reason_code: ReasonCode
    delta_paise: int


@dataclass
class SampleBatch:
    """A generated batch plus the ground truth about what is wrong with it."""

    settlement: dict
    transactions: pd.DataFrame
    line_items: pd.DataFrame
    injected_defects: list[InjectedDefect] = field(default_factory=list)

    @property
    def expected_exception_count(self) -> int:
        return len(self.injected_defects)


class SampleDataProvider:
    """Generates deterministic sample settlements. No network, no credentials."""

    def __init__(self, rules: Rules, seed: int = 42) -> None:
        self._rules = rules
        self._seed = seed
        self._batches: dict[str, SampleBatch] = {}
        # Ground-truth statuses for Module 2, keyed by payment id.
        self._payment_truth: dict[str, str] = {}

    # -- generation ---------------------------------------------------------

    def generate(
        self,
        settlement_id: str = "setl_DEMO001",
        n_transactions: int = 250,
        defect_rate: float = 0.04,
    ) -> SampleBatch:
        """Build one labelled settlement batch.

        ``defect_rate`` is the fraction of transactions given a planted defect.
        The default 4% leaves accuracy near 96%, in the region the PRD's >=95%
        target describes, without making the batch implausibly clean.
        """
        rng = random.Random(self._seed)
        base_time = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)

        methods = list(METHOD_WEIGHTS)
        weights = list(METHOD_WEIGHTS.values())

        txn_rows: list[dict] = []
        item_rows: list[dict] = []
        defects: list[InjectedDefect] = []
        net_total = 0

        for i in range(n_transactions):
            txn_id = f"pay_S{i:05d}"
            method = rng.choices(methods, weights=weights, k=1)[0]
            # Amounts skewed low, as real D2C order values are.
            amount = rng.choice([49_900, 89_900, 129_900, 249_900, 499_900,
                                 1_299_00, 2_499_00, 9_999_00])
            created = base_time + timedelta(minutes=rng.randint(0, 900))

            expected = compute_expected_fees(amount, method, self._rules)
            fee, gst, reserve = (
                expected.fee_paise,
                expected.gst_paise,
                expected.reserve_paise,
            )
            settlement_ref: str | None = settlement_id
            status = "captured"
            defect = self._maybe_inject(rng, defect_rate, txn_id, amount, fee)

            if defect is not None:
                defects.append(defect)
                if defect.reason_code is ReasonCode.FEE_MISMATCH:
                    fee += defect.delta_paise
                elif defect.reason_code is ReasonCode.GST_MISMATCH:
                    gst += defect.delta_paise
                elif defect.reason_code is ReasonCode.UNMATCHED_TRANSACTION:
                    # Captured, but this payout does not account for it at all.
                    settlement_ref = None
                elif defect.reason_code is ReasonCode.LATE_AUTHORIZATION_PENDING:
                    settlement_ref = None
                    status = "authorized"

            txn_rows.append(
                {
                    "transaction_id": txn_id,
                    "order_id": f"order_S{i:05d}",
                    "amount_paise": amount,
                    "currency": "INR",
                    "method": method,
                    "status": status,
                    "created_at": created,
                    "captured_at": created + timedelta(seconds=rng.randint(2, 40))
                    if status == "captured"
                    else None,
                    "settlement_id": settlement_ref,
                }
            )

            if settlement_ref is not None:
                item_rows.append(
                    {
                        "transaction_id": txn_id,
                        "settlement_id": settlement_id,
                        "fee_paise": fee,
                        "gst_paise": gst,
                        "reserve_paise": reserve,
                    }
                )
                net_total += amount - (fee + gst + reserve)

        settlement = {
            "settlement_id": settlement_id,
            "utr": f"UTR{rng.randint(10**11, 10**12 - 1)}",
            "amount_paise": net_total,
            "fees_paise": sum(r["fee_paise"] for r in item_rows),
            "tax_paise": sum(r["gst_paise"] for r in item_rows),
            "settled_at": base_time + timedelta(days=1),
        }

        batch = SampleBatch(
            settlement=settlement,
            transactions=pd.DataFrame(txn_rows),
            line_items=pd.DataFrame(item_rows),
            injected_defects=defects,
        )
        self._batches[settlement_id] = batch
        return batch

    def _maybe_inject(
        self, rng: random.Random, rate: float, txn_id: str, amount: int, fee: int
    ) -> InjectedDefect | None:
        if rng.random() >= rate:
            return None

        kind = rng.choices(
            [
                ReasonCode.FEE_MISMATCH,
                ReasonCode.GST_MISMATCH,
                ReasonCode.UNMATCHED_TRANSACTION,
                ReasonCode.LATE_AUTHORIZATION_PENDING,
            ],
            weights=[0.45, 0.20, 0.20, 0.15],
            k=1,
        )[0]

        if kind is ReasonCode.FEE_MISMATCH:
            # An overcharge large enough to matter, scaled to the transaction.
            delta = max(50, int(amount * rng.uniform(0.002, 0.008)))
        elif kind is ReasonCode.GST_MISMATCH:
            delta = max(10, int(fee * rng.uniform(0.05, 0.20)))
        else:
            delta = amount

        return InjectedDefect(
            transaction_id=txn_id, reason_code=kind, delta_paise=delta
        )

    # -- stuck orders for Module 2 -----------------------------------------

    def generate_stuck_orders(self, n_stuck: int = 3, n_healthy: int = 12) -> pd.DataFrame:
        """Orders for the Payment State Reconciler demo.

        The stuck ones are the scenario from PRD 2.2: the payment really did
        succeed on Razorpay's side, but the webhook never landed, so the
        merchant's record still says pending. ``fetch_payment_status`` returns
        the true status, which is what makes the correction demonstrable.
        """
        rng = random.Random(self._seed + 1)
        now = datetime.now(timezone.utc)
        rows: list[dict] = []

        for i in range(n_stuck):
            order_id = f"order_STUCK{i:03d}"
            payment_id = f"pay_STUCK{i:03d}"
            method = rng.choice(["upi", "card", "netbanking"])
            # Created well beyond the resolution window for its method.
            window = self._rules.resolution_window_for(method)
            created = now - timedelta(seconds=window + rng.randint(600, 7200))
            rows.append(
                {
                    "order_id": order_id,
                    "payment_id": payment_id,
                    "amount_paise": rng.choice([49_900, 129_900, 499_900]),
                    "method": method,
                    "local_status": "pending",
                    "created_at": created,
                }
            )
            # Razorpay's truth: it was captured all along.
            self._payment_truth[payment_id] = "captured"

        for i in range(n_healthy):
            order_id = f"order_OK{i:03d}"
            payment_id = f"pay_OK{i:03d}"
            method = rng.choice(["upi", "card"])
            created = now - timedelta(seconds=rng.randint(10, 120))
            rows.append(
                {
                    "order_id": order_id,
                    "payment_id": payment_id,
                    "amount_paise": rng.choice([49_900, 89_900]),
                    "method": method,
                    "local_status": "captured" if i % 3 else "pending",
                    "created_at": created,
                }
            )
            self._payment_truth[payment_id] = "captured" if i % 3 else "pending"

        return pd.DataFrame(rows)

    def injected_defects_for(
        self, settlement_id: str
    ) -> list[tuple[str, ReasonCode]] | None:
        """Ground truth for a generated batch, as (transaction_id, reason) pairs.

        This is what makes FR-1.6's precision and recall a measurement rather
        than a claim. Returns None for a settlement this provider never
        generated, so a caller cannot mistake "no defects planted" for "this
        batch was never labelled".
        """
        batch = self._batches.get(settlement_id)
        if batch is None:
            return None
        return [(d.transaction_id, d.reason_code) for d in batch.injected_defects]

    # -- DataProvider interface --------------------------------------------

    def fetch_settlement(self, settlement_id: str) -> dict:
        batch = self._batches.get(settlement_id) or self.generate(settlement_id)
        return batch.settlement

    def fetch_transactions(self, start: datetime, end: datetime) -> pd.DataFrame:
        frames = [b.transactions for b in self._batches.values()]
        if not frames:
            frames = [self.generate().transactions]
        txns = pd.concat(frames, ignore_index=True)
        mask = (txns["created_at"] >= start) & (txns["created_at"] <= end)
        return txns[mask].reset_index(drop=True)

    def fetch_line_items(self, settlement_id: str) -> pd.DataFrame:
        batch = self._batches.get(settlement_id) or self.generate(settlement_id)
        return batch.line_items

    def fetch_payment_status(self, payment_id: str) -> str:
        """Ground-truth status for one payment (FR-2.3).

        Derived from the payment ID rather than read from generator state. The
        API server and the seeding script are separate processes, so anything
        held only in ``_payment_truth`` is invisible to the server that actually
        serves the re-check - which silently made every stuck order look
        genuinely unresolved instead of desynced. The in-memory map still wins
        when present, so a test can script a specific status.
        """
        if payment_id in self._payment_truth:
            return self._payment_truth[payment_id]

        # Seeded stuck orders are the PRD 2.2 scenario: captured on Razorpay's
        # side, still pending locally because the webhook never arrived.
        if payment_id.startswith("pay_STUCK"):
            return "captured"
        return "pending"
