"""Background re-check loop for the Payment State Reconciler (FR-2.2).

The backoff schedule in ``config/rules.yaml`` only means anything if something
drives it. This module runs an asyncio task alongside the API that wakes every
few seconds, finds the orders whose next re-check has come due, and polls
Razorpay for each - so an order that goes stale overnight is corrected
overnight, with no operator present to click anything.

The loop deliberately does not decide *when* each order is next checked; that
stays in :mod:`settletrace.reconciler_service`, which owns the backoff rules.
This module only supplies the heartbeat.

State is exposed through :data:`state` so the UI can show the automation
running. A background process an operator cannot observe is indistinguishable
from one that has quietly died.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import get_rules
from .db import session_scope
from .models import utcnow
from .providers import get_provider
from .reconciler_service import run_reconciliation_cycle

logger = logging.getLogger(__name__)

# How often the loop wakes. This is the heartbeat, not the backoff interval:
# the per-order schedule (30s, 2min, 10min, capped) lives in the rules config,
# and a tick simply asks which orders have come due since the last one.
TICK_SECONDS = 15

# How many recent ticks to keep for the UI. Bounded so a long-running process
# cannot accumulate history without limit.
RECENT_TICKS = 20


@dataclass
class SchedulerState:
    """Observable state of the loop, read by ``GET /scheduler/status``."""

    running: bool = False
    tick_seconds: int = TICK_SECONDS
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    total_ticks: int = 0
    total_rechecked: int = 0
    total_corrected: int = 0
    last_error: str | None = None
    recent: deque = field(default_factory=lambda: deque(maxlen=RECENT_TICKS))

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "tick_seconds": self.tick_seconds,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "total_ticks": self.total_ticks,
            "total_rechecked": self.total_rechecked,
            "total_corrected": self.total_corrected,
            "last_error": self.last_error,
            "recent": list(self.recent),
        }


state = SchedulerState()

_task: asyncio.Task | None = None


def _run_one_cycle() -> dict:
    """One detect-and-recheck pass, in its own transaction.

    Runs synchronously in a worker thread: the reconciliation path is blocking
    SQLAlchemy and blocking HTTP, and calling it directly on the event loop
    would stall every in-flight API request for the duration of the poll.
    """
    with session_scope() as session:
        return run_reconciliation_cycle(session, get_provider(), get_rules())


async def _loop() -> None:
    """Tick until cancelled."""
    logger.info("Re-check scheduler started (tick every %ds)", TICK_SECONDS)
    state.running = True
    state.next_run_at = utcnow() + timedelta(seconds=TICK_SECONDS)

    try:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            started = utcnow()

            try:
                result = await asyncio.to_thread(_run_one_cycle)
                state.last_error = None
                state.total_rechecked += result["rechecked"]
                state.total_corrected += result["corrected"]
                state.recent.appendleft(
                    {
                        "at": started.isoformat(),
                        "newly_stuck": result["newly_stuck"],
                        "rechecked": result["rechecked"],
                        "corrected": result["corrected"],
                    }
                )
                if result["rechecked"]:
                    logger.info(
                        "Scheduler tick: %d rechecked, %d corrected",
                        result["rechecked"],
                        result["corrected"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A failed tick must not kill the loop. Razorpay being briefly
                # unreachable is an expected condition, and a scheduler that
                # stopped at the first 503 would silently stop correcting
                # anything for the rest of the process's life.
                state.last_error = str(exc)
                logger.exception("Scheduler tick failed; continuing")

            state.total_ticks += 1
            state.last_run_at = started
            state.next_run_at = utcnow() + timedelta(seconds=TICK_SECONDS)

    except asyncio.CancelledError:
        logger.info("Re-check scheduler stopping")
        raise
    finally:
        state.running = False
        state.next_run_at = None


def start() -> None:
    """Start the loop if it is not already running."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="settletrace-recheck-scheduler")


async def stop() -> None:
    """Cancel the loop and wait for it to unwind."""
    global _task
    if _task is None:
        return

    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    finally:
        _task = None
        state.running = False
