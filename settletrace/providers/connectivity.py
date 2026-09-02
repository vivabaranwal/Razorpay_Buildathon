"""Live connectivity check against Razorpay's sandbox API.

This exists so the system is demonstrably not entirely simulated. It makes one
real, read-only HTTPS call to Razorpay with the configured test-mode
credentials and reports exactly what came back - including, honestly, when
nothing did.

Read-only by design: it lists payments rather than creating anything, so
running it against a real account can have no side effect. It is also the
cheapest way to distinguish the three failures that otherwise look identical
from the dashboard - no credentials configured, credentials rejected, and
credentials fine but the account has no data yet.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import Settings

logger = logging.getLogger(__name__)

# One probe should never hang a page load. Razorpay's API is normally well
# under a second; beyond this the answer for the operator is the same either
# way - it is not usable right now.
PROBE_TIMEOUT_SECONDS = 10


@dataclass
class ProbeResult:
    """Outcome of one live call, shaped for display rather than for control flow."""

    reachable: bool
    detail: str
    checked_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    latency_ms: int | None = None
    endpoint: str = "GET /v1/payments"
    payments_visible: int | None = None
    sample_payment_id: str | None = None
    error_kind: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "detail": self.detail,
            "checked_at": self.checked_at,
            "latency_ms": self.latency_ms,
            "endpoint": self.endpoint,
            "payments_visible": self.payments_visible,
            "sample_payment_id": self.sample_payment_id,
            "error_kind": self.error_kind,
        }


def _classify(exc: Exception) -> tuple[str, str]:
    """Turn an SDK exception into an operator-actionable kind and message.

    The SDK raises a handful of loosely-typed errors, and the raw text is
    frequently a bare status code. Mapping them here means the dashboard can
    say what to actually do about it.
    """
    text = str(exc)
    lowered = text.lower()

    if "401" in text or "authentication" in lowered or "unauthorized" in lowered:
        return (
            "auth_failed",
            "Razorpay rejected the credentials. Check RAZORPAY_KEY_ID and "
            "RAZORPAY_KEY_SECRET are a matching test-mode pair.",
        )
    if "429" in text or "rate limit" in lowered:
        return ("rate_limited", "Razorpay rate-limited the request. Try again shortly.")
    if any(code in text for code in ("500", "502", "503", "504")):
        return ("server_error", f"Razorpay returned a server error: {text}")
    if any(word in lowered for word in ("timeout", "timed out")):
        return ("timeout", "The request to Razorpay timed out.")
    if any(word in lowered for word in ("connection", "resolve", "network", "ssl")):
        return (
            "unreachable",
            "Could not reach Razorpay. Check network connectivity.",
        )
    return ("unknown", f"Unexpected error calling Razorpay: {text}")


def probe_razorpay(settings: Settings | None = None) -> ProbeResult:
    """Make one real read-only call to Razorpay and report what happened."""
    from ..config import get_settings

    settings = settings or get_settings()

    if not settings.razorpay_configured:
        return ProbeResult(
            reachable=False,
            detail=(
                "No Razorpay credentials configured. Set RAZORPAY_KEY_ID and "
                "RAZORPAY_KEY_SECRET in .env to enable live calls."
            ),
            error_kind="not_configured",
        )

    try:
        import razorpay
    except ImportError:
        return ProbeResult(
            reachable=False,
            detail="The razorpay package is not installed (pip install razorpay).",
            error_kind="sdk_missing",
        )

    client = razorpay.Client(
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
    )
    client.set_app_details({"title": "SettleTrace", "version": "1.0.0"})

    started = time.perf_counter()
    try:
        # count=1 keeps the probe cheap; we want proof of a round trip and of
        # accepted credentials, not the merchant's payment history.
        response = client.payment.all({"count": 1})
    except Exception as exc:
        kind, message = _classify(exc)
        logger.warning("Razorpay probe failed (%s): %s", kind, exc)
        return ProbeResult(
            reachable=False,
            detail=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_kind=kind,
        )

    latency_ms = int((time.perf_counter() - started) * 1000)
    items = response.get("items", []) if isinstance(response, dict) else []
    count = response.get("count", len(items)) if isinstance(response, dict) else 0

    if not items:
        # Credentials work; the account simply has no payments yet. Worth
        # distinguishing, because it is the normal state of a fresh test
        # account and is not a failure of this system.
        return ProbeResult(
            reachable=True,
            detail=(
                "Connected to Razorpay. The test account has no payments yet, "
                "so there is nothing to reconcile from live data."
            ),
            latency_ms=latency_ms,
            payments_visible=0,
        )

    return ProbeResult(
        reachable=True,
        detail=(
            f"Connected to Razorpay and read {count} payment(s) from the "
            "test-mode account."
        ),
        latency_ms=latency_ms,
        payments_visible=count,
        sample_payment_id=items[0].get("id"),
    )
