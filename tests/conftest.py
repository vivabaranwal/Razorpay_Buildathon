"""Shared test fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from settletrace.config import Rules
from settletrace.models import Base


# Blank every provider credential before anything imports Settings.
#
# This runs at import time rather than in a fixture: pytest imports test
# modules - and therefore settletrace.config - before any fixture executes, so
# a session-scoped fixture would fire after the first Settings had already read
# the real .env.
#
# Without it, a developer with real keys runs a different suite from CI: the
# API tests trigger batches, and every exception in them would make a live LLM
# call. That is how the suite went from 8 seconds to hanging once real keys
# were added to .env.
for _credential in (
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "ANTHROPIC_API_KEY",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
):
    os.environ[_credential] = ""
os.environ["SETTLETRACE_USE_SANDBOX"] = "false"



@pytest.fixture
def rules() -> Rules:
    return Rules(
        fee_rates={"default": {"upi": 0.0, "card": 0.02, "netbanking": 0.0175}},
        gst_rate=0.18,
        reserve_rates={"default": 0.0},
        match_tolerance_paise=100,
        fee_tolerance_paise=1,
        resolution_windows_seconds={"upi": 300, "card": 900, "default": 900},
        backoff_schedule_seconds=[30, 120, 600, 1800],
        backoff_max_attempts=4,
        webhook_event_retention_hours=24,
    )


@pytest.fixture
def session() -> Iterator[Session]:
    """An in-memory database, isolated per test."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
