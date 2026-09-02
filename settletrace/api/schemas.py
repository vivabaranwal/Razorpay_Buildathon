"""Response models for the API.

Amounts are exposed in both paise and rupees: paise is the authoritative
integer the engine reconciled on, and rupees is what a merchant reads. Sending
only rupees would push float rounding into every client.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, computed_field

from ..models import ExplanationSource, MatchStatus, PaymentStatus, ReasonCode


class BatchSummary(BaseModel):
    """Throughput and accuracy for one batch (FR-1.6, FR-4.1)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    settlement_id: str
    started_at: datetime
    completed_at: datetime | None
    transactions_processed: int
    transactions_verified: int
    transactions_exception: int
    accuracy: float | None
    is_labeled: bool

    precision: float | None = None
    recall: float | None = None
    true_positives: int | None = None
    false_positives: int | None = None
    false_negatives: int | None = None

    @computed_field
    @property
    def accuracy_pct(self) -> float | None:
        return round(self.accuracy * 100, 2) if self.accuracy is not None else None

    @computed_field
    @property
    def precision_pct(self) -> float | None:
        return round(self.precision * 100, 2) if self.precision is not None else None

    @computed_field
    @property
    def recall_pct(self) -> float | None:
        return round(self.recall * 100, 2) if self.recall is not None else None

    @computed_field
    @property
    def f1_pct(self) -> float | None:
        """Harmonic mean of precision and recall, as a percentage.

        Reported alongside both components rather than instead of them: a single
        blended figure hides which of the two failure modes a batch is suffering
        from, and they call for opposite fixes.
        """
        if self.precision is None or self.recall is None:
            return None
        total = self.precision + self.recall
        if not total:
            return 0.0
        return round(2 * self.precision * self.recall / total * 100, 2)

    @computed_field
    @property
    def accuracy_is_measured(self) -> bool:
        """Whether this accuracy figure may be quoted as a measured result.

        FR-1.6's business rule: accuracy is only a measurement when the batch
        had an independently known correct answer. Clients render the
        distinction rather than presenting every number as verified fact.
        """
        return self.is_labeled


class ExceptionOut(BaseModel):
    """One reconciliation exception (FR-1.5, FR-3.1, FR-3.2)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_id: int
    reason_code: ReasonCode
    transaction_id: str | None
    settlement_id: str | None
    expected_paise: int
    actual_paise: int
    delta_paise: int
    explanation_text: str | None
    explanation_source: ExplanationSource | None
    impact_rank: int | None
    impact_score: int
    resolved_flag: bool
    resolved_at: datetime | None
    resolved_by: str | None

    @computed_field
    @property
    def delta_inr(self) -> float:
        return round(self.delta_paise / 100, 2)

    @computed_field
    @property
    def impact_inr(self) -> float:
        return round(self.impact_score / 100, 2)

    @computed_field
    @property
    def is_ai_explained(self) -> bool:
        """Whether the explanation came from the model or a template.

        Sent as an explicit boolean rather than left for the client to infer
        from ``explanation_source``, so that a client which forgets to check
        cannot default into presenting template text as AI output.
        """
        return self.explanation_source is ExplanationSource.LLM


class StuckOrderOut(BaseModel):
    """An order whose payment state may have gone stale (FR-2.1)."""

    model_config = ConfigDict(from_attributes=True)

    order_id: str
    payment_id: str | None
    amount_paise: int
    method: str
    local_status: PaymentStatus
    created_at: datetime
    is_stuck_candidate: bool
    recheck_attempts: int
    next_recheck_at: datetime | None

    @computed_field
    @property
    def amount_inr(self) -> float:
        return round(self.amount_paise / 100, 2)

    @computed_field
    @property
    def seconds_stuck(self) -> int:
        """How long this order has sat in a non-terminal state.

        Computed server-side from the stored timestamp rather than in the
        browser: the client's clock may be wrong or in another timezone, and
        "how overdue is this" is exactly the figure that must not be.
        """
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - created).total_seconds()))


class RecheckResult(BaseModel):
    """Outcome of polling Razorpay for one order (FR-2.3, FR-2.4)."""

    order_id: str
    previous_status: PaymentStatus
    actual_status: PaymentStatus
    corrected: bool
    checked_at: datetime


class MatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    settlement_id: str
    match_status: MatchStatus
    expected_net_paise: int
    actual_net_paise: int


class BatchRequest(BaseModel):
    """Trigger a reconciliation batch (FR-1.1)."""

    settlement_id: str = "setl_DEMO001"
    explain: bool = True


class ResolveRequest(BaseModel):
    resolved_by: str = "merchant_user"


class WebhookAck(BaseModel):
    """Whether a webhook was processed or discarded as a duplicate (FR-2.5)."""

    event_id: str
    processed: bool
    duplicate: bool


class AuditLogOut(BaseModel):
    """One row of the append-only audit trail.

    Read-only by design: there is no corresponding request model, because
    nothing outside the system may write to this table.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: str
    field_changed: str
    old_value: str | None
    new_value: str | None
    changed_by: str
    changed_at: datetime
    reason: str | None

    @computed_field
    @property
    def is_automatic(self) -> bool:
        """Whether the system made this change or a named person did."""
        return self.changed_by == "system"


class SchedulerStatus(BaseModel):
    """Heartbeat of the background re-check loop (FR-2.2).

    Surfaced so the dashboard can show the automation running. An automated
    process the operator cannot observe is indistinguishable from one that has
    quietly died.
    """

    running: bool
    tick_seconds: int
    last_run_at: datetime | None
    next_run_at: datetime | None
    total_ticks: int
    total_rechecked: int
    total_corrected: int
    last_error: str | None
    recent: list[dict]


class HealthOut(BaseModel):
    """System status, including the two facts that change how results read."""

    status: str
    data_source: str
    data_source_is_live: bool
    llm_explanations: str
    llm_configured: bool
    scheduler_running: bool

    # Set when live data was requested but is unusable. The client renders this
    # as a persistent banner: the system degrades to generated data rather than
    # failing, so it has to say loudly that is what happened.
    data_degraded: bool = False
    degraded_reason: str | None = None


class ConnectivityOut(BaseModel):
    """Result of one live, read-only call to Razorpay's sandbox API.

    Surfaced so the system can demonstrate it is not entirely simulated, and so
    the three failures that otherwise look alike from the UI - no credentials,
    rejected credentials, and a working account with no data - are told apart.
    """

    reachable: bool
    detail: str
    checked_at: datetime
    latency_ms: int | None
    endpoint: str
    payments_visible: int | None
    sample_payment_id: str | None
    error_kind: str | None
