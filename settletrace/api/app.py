"""SettleTrace REST API - the endpoints specified in FRS section 9."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .. import scheduler
from ..audit import read_trail
from ..batch_service import resolve_exception, run_settlement_batch
from ..config import get_rules, get_settings
from ..db import get_session, init_db, session_scope
from ..explainer import llm_health
from ..export import export_csv, export_filename, export_json
from ..models import AuditLog, Batch, Exception_, Match, Order, ReasonCode
from ..providers import get_provider
from ..providers.connectivity import probe_razorpay
from ..providers.factory import degraded_state
from ..startup import log_credential_report
from ..reconciler_service import (
    detect_stuck_candidates,
    recheck_order,
    record_webhook_event,
    run_reconciliation_cycle,
)
from .schemas import (
    AuditLogOut,
    BatchRequest,
    BatchSummary,
    ConnectivityOut,
    ExceptionOut,
    HealthOut,
    MatchOut,
    RecheckResult,
    ResolveRequest,
    SchedulerStatus,
    StuckOrderOut,
    WebhookAck,
)

logger = logging.getLogger(__name__)


def _seed_if_empty() -> None:
    """Seed the demo dataset, but only into an empty database.

    Imported lazily: reset_demo lives at the project root and pulls in the
    sample generator, which the API has no reason to load when seeding is off.
    Failure here is logged and swallowed - an unseeded dashboard is a poor
    demo, but a backend that will not boot is a dead one.
    """
    try:
        with session_scope() as session:
            if session.execute(select(Batch.id).limit(1)).first() is not None:
                logger.info("SEED_ON_BOOT set, but data already present - skipping")
                return

        from reset_demo import reset

        result = reset()
        logger.info(
            "Seeded demo data: batch #%s, %s transactions, %s exceptions",
            result["batch_id"],
            result["processed"],
            result["exceptions"],
        )
    except Exception:  # noqa: BLE001 - boot must survive a seeding failure
        logger.exception("Boot seeding failed; starting with an empty database")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Uvicorn configures logging only for its own loggers, so an app-level
    # logger would otherwise emit nothing and the credential report would be
    # invisible - the exact opposite of the point.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    log_credential_report()
    init_db()

    # A hosted deployment starts on an empty, ephemeral disk, so the dashboard
    # would greet its first visitor with "no batches yet" and nothing to click.
    # Seeding is opt-in via SEED_ON_BOOT so local runs keep whatever state the
    # developer left behind, and it only fires when the database is genuinely
    # empty - a restart must never discard work someone did on the live site.
    if os.getenv("SEED_ON_BOOT", "").strip().lower() in {"1", "true", "yes"}:
        _seed_if_empty()
    # FR-2.2's re-check schedule is only real if something drives it. The loop
    # starts with the app rather than on a button, so an order that goes stale
    # overnight is corrected overnight.
    scheduler.start()
    logger.info("SettleTrace API ready")
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(
    title="SettleTrace",
    description="Reconciliation copilot for Razorpay merchants",
    version="1.0.0",
    lifespan=lifespan,
)

# The React client is served from its own origin, in development and in
# production alike. Origins are listed explicitly rather than wildcarded: this
# API exposes a merchant's settlement data, and a wildcard would let any page
# in the browser read it.
#
# CORS_ALLOW_ORIGINS holds the deployed frontend's URL (comma-separated if
# there is more than one). Vercel preview deployments get a fresh subdomain per
# build, which cannot be enumerated ahead of time, so they are matched by
# pattern instead - deliberately anchored, so only *.vercel.app over HTTPS
# matches and not, say, https://vercel.app.evil.com.
_dev_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]
_configured_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
]
VERCEL_PREVIEW_ORIGIN = r"https://[a-z0-9-]+\.vercel\.app"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_dev_origins + _configured_origins,
    allow_origin_regex=VERCEL_PREVIEW_ORIGIN,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # The download filename lives in this header; without exposing it the
    # browser hides it from fetch() and every export saves as "download".
    expose_headers=["Content-Disposition"],
)


@app.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """Liveness plus the facts that change how results should be read.

    Both provenance flags are part of the health payload rather than buried in
    a footnote, because the client renders them as a persistent status pill. A
    reader must never have to guess whether a number came from Razorpay or from
    generated data, or whether an explanation came from a model or a template.
    """
    settings = get_settings()

    # Ask for the provider so a degraded fallback is detected here rather than
    # first surfacing when the operator triggers a batch mid-demo.
    try:
        get_provider()
    except Exception:
        logger.exception("Provider initialisation failed during health check")

    llm_live = settings.llm_configured and llm_health.is_usable
    degraded = degraded_state.degraded
    # Live only if it was asked for *and* actually achieved. Reporting the flag
    # alone would badge generated data as coming from Razorpay.
    live_data = settings.settletrace_use_sandbox and not degraded

    return HealthOut(
        status="ok",
        data_source="razorpay_sandbox" if live_data else "generated_sample",
        data_source_is_live=live_data,
        # "Connected" requires a key that is configured *and* not known to be
        # rejected. A key alone is a claim, not evidence, and badging an
        # unusable key as connected would put "AI-generated" beside template
        # text - the one disclosure failure the design rules out.
        llm_explanations="live" if llm_live else "fallback_templates",
        llm_configured=llm_live,
        scheduler_running=scheduler.state.running,
        data_degraded=degraded,
        degraded_reason=degraded_state.reason,
    )


# --- Module 1: settlement reconciliation -----------------------------------


@app.post("/batches/settlement", response_model=BatchSummary, status_code=201)
def trigger_settlement_batch(
    request: BatchRequest, session: Session = Depends(get_session)
) -> Batch:
    """Run a settlement reconciliation batch (FR-1.1 to FR-1.6)."""
    settings = get_settings()
    rules = get_rules()
    provider = get_provider()

    # Sample batches carry a known correct answer, so their accuracy is a
    # measurement; sandbox batches do not, and must not be quoted as one.
    is_labeled = not settings.settletrace_use_sandbox

    try:
        batch = run_settlement_batch(
            session=session,
            settlement_id=request.settlement_id,
            provider=provider,
            rules=rules,
            settings=settings,
            explain=request.explain,
            is_labeled=is_labeled,
        )
    except ValueError as exc:
        # A malformed or unknown settlement identifier is the caller's error;
        # FR-1.1 requires a clear validation response rather than a silent fail.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Anything else - an upstream fetch failing part-way through, most
        # likely - is reported as a readable 502 rather than an unhandled 500.
        # The session is rolled back so a half-written batch cannot be mistaken
        # for a complete one.
        session.rollback()
        logger.exception("Settlement batch failed for %s", request.settlement_id)
        raise HTTPException(
            status_code=502,
            detail=(
                f"Reconciliation could not complete: {exc}. No partial batch "
                "was saved."
            ),
        ) from exc

    session.commit()
    return batch


@app.get("/batches/{batch_id}/summary", response_model=BatchSummary)
def batch_summary(batch_id: int, session: Session = Depends(get_session)) -> Batch:
    """Throughput and accuracy for a batch (FR-1.6, FR-4.1)."""
    batch = session.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"No batch {batch_id}")
    return batch


@app.get("/batches", response_model=list[BatchSummary])
def list_batches(
    limit: int = Query(20, ge=1, le=100), session: Session = Depends(get_session)
) -> list[Batch]:
    """Recent batches, newest first - the dashboard's 'latest batch' source."""
    return list(
        session.scalars(select(Batch).order_by(desc(Batch.id)).limit(limit)).all()
    )


@app.get("/batches/{batch_id}/matches", response_model=list[MatchOut])
def batch_matches(
    batch_id: int, session: Session = Depends(get_session)
) -> list[Match]:
    return list(
        session.scalars(select(Match).where(Match.batch_id == batch_id)).all()
    )


@app.get("/exceptions", response_model=list[ExceptionOut])
def list_exceptions(
    batch_id: int | None = None,
    reason_code: ReasonCode | None = None,
    search: str | None = None,
    include_resolved: bool = False,
    limit: int = Query(500, ge=1, le=5000),
    session: Session = Depends(get_session),
) -> list[Exception_]:
    """List exceptions, filtered and searchable (FR-4.2).

    Ordered by impact rank so the highest-value discrepancies come first
    (FR-3.2). Filtering narrows the view only - FR-4.2's business rule is that
    it must never remove anything from the underlying data.
    """
    stmt = select(Exception_)

    if batch_id is not None:
        stmt = stmt.where(Exception_.batch_id == batch_id)
    if reason_code is not None:
        stmt = stmt.where(Exception_.reason_code == reason_code)
    if not include_resolved:
        stmt = stmt.where(Exception_.resolved_flag.is_(False))
    if search:
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            Exception_.transaction_id.like(term)
            | Exception_.settlement_id.like(term)
        )

    stmt = stmt.order_by(Exception_.impact_rank.is_(None), Exception_.impact_rank)
    return list(session.scalars(stmt.limit(limit)).all())


@app.post("/exceptions/{exception_id}/resolve", response_model=ExceptionOut)
def resolve(
    exception_id: int,
    request: ResolveRequest,
    session: Session = Depends(get_session),
) -> Exception_:
    """Mark an exception resolved by a human reviewer (UC-2).

    A reviewer name is required, not defaulted. The audit row this writes is
    only worth having if it records who actually signed the item off, and a
    silent default would fill the trail with an anonymous placeholder.
    """
    reviewer = (request.resolved_by or "").strip()
    if not reviewer:
        raise HTTPException(
            status_code=422,
            detail="resolved_by is required: the audit trail records who closed this.",
        )

    row = resolve_exception(session, exception_id, reviewer)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No exception {exception_id}")
    session.commit()
    return row


@app.get("/batches/{batch_id}/export")
def export_batch(
    batch_id: int,
    format: str = Query("csv", pattern="^(csv|json)$"),
    session: Session = Depends(get_session),
) -> Response:
    """Download the batch summary and exception list as a file.

    The artifact outlives the session that produced it: a merchant querying a
    deduction with Razorpay needs something to attach to an email, and month-end
    close needs something to open in a spreadsheet.
    """
    batch = session.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"No batch {batch_id}")

    if format == "json":
        body, media = export_json(session, batch), "application/json"
    else:
        body, media = export_csv(session, batch), "text/csv"

    filename = export_filename(batch, format)
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Module 2: payment state reconciliation --------------------------------


@app.get("/orders/stuck", response_model=list[StuckOrderOut])
def stuck_orders(session: Session = Depends(get_session)) -> list[Order]:
    """Current stuck-candidate orders (FR-2.1).

    Detection runs on read so the list reflects elapsed time at the moment it
    is asked for, rather than whenever a scheduler last happened to run.
    """
    detect_stuck_candidates(session, get_rules())
    session.commit()
    return list(
        session.scalars(
            select(Order).where(Order.is_stuck_candidate.is_(True))
        ).all()
    )


@app.post("/orders/{order_id}/recheck", response_model=RecheckResult)
def recheck(order_id: str, session: Session = Depends(get_session)) -> RecheckResult:
    """Immediately poll Razorpay for one order (FR-2.2 to FR-2.4)."""
    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"No order {order_id}")
    if not order.payment_id:
        raise HTTPException(
            status_code=400,
            detail=f"Order {order_id} has no payment attached to poll",
        )

    try:
        check = recheck_order(session, order, get_provider(), get_rules())
    except Exception as exc:
        session.rollback()
        logger.exception("Re-check failed for order %s", order_id)
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach Razorpay to re-check {order_id}: {exc}",
        ) from exc
    session.commit()

    return RecheckResult(
        order_id=order_id,
        previous_status=check.expected_status,
        actual_status=check.actual_status_from_api,
        corrected=check.corrected_flag,
        checked_at=check.checked_at,
    )


@app.post("/orders/reconcile")
def reconcile_orders(session: Session = Depends(get_session)) -> dict:
    """Run one full detect-and-recheck cycle (UC-3)."""
    result = run_reconciliation_cycle(session, get_provider(), get_rules())
    session.commit()
    return result


@app.post("/webhooks/razorpay", response_model=WebhookAck)
def razorpay_webhook(
    payload: dict,
    x_razorpay_event_id: str = Header(...),
    session: Session = Depends(get_session),
) -> WebhookAck:
    """Receive a Razorpay webhook, discarding duplicates (FR-2.5).

    A duplicate is acknowledged with 200, not an error: Razorpay is behaving
    correctly when it retries, and a non-2xx would make it retry again.
    """
    event_type = payload.get("event", "unknown")
    payment_id = (
        payload.get("payload", {})
        .get("payment", {})
        .get("entity", {})
        .get("id")
    )

    is_new = record_webhook_event(
        session, x_razorpay_event_id, event_type, payment_id
    )
    session.commit()

    return WebhookAck(
        event_id=x_razorpay_event_id, processed=is_new, duplicate=not is_new
    )


# --- audit trail and scheduler visibility ----------------------------------


@app.get("/audit-log", response_model=list[AuditLogOut])
def audit_log(
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> list[AuditLog]:
    """The append-only trail of every state change, newest first.

    Read-only: there is deliberately no endpoint that writes to or edits this
    table. A trail that callers can amend is not evidence of anything.
    """
    return read_trail(session, entity_type, entity_id, limit)


@app.get("/scheduler/status", response_model=SchedulerStatus)
def scheduler_status() -> SchedulerStatus:
    """Heartbeat of the automatic re-check loop (FR-2.2)."""
    return SchedulerStatus(**scheduler.state.as_dict())


@app.get("/connectivity/razorpay", response_model=ConnectivityOut)
def razorpay_connectivity() -> ConnectivityOut:
    """Make one real read-only call to Razorpay and report the result.

    Deliberately a separate endpoint rather than part of ``/health``: it is a
    live network round trip, and folding it into the health check would put an
    external dependency in the path of every page load.
    """
    return ConnectivityOut(**probe_razorpay().as_dict())
