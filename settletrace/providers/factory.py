"""Provider selection, with a degraded-but-alive fallback.

The sample provider is the default. Running the demo must not depend on
credentials or network reachability.

When sandbox mode is requested but unusable, this module falls back to
generated data rather than raising. An earlier version raised instead, on the
principle that an operator who asked for live data must never be shown
generated numbers believing they came from Razorpay. That principle is right,
but a crash is the wrong mechanism for it: ``get_provider`` is called inside
request handlers, so a raise there is a 500 and a dead dashboard.

The honesty guarantee is kept by :data:`degraded_state` instead - the API
reports it, and the client shows a persistent banner naming exactly what went
wrong. The system stays up and stays honest.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from ..config import Rules, Settings, get_rules, get_settings
from .base import DataProvider
from .sample import SampleDataProvider

logger = logging.getLogger(__name__)


@dataclass
class DegradedState:
    """Why live data is unavailable, if it is. Surfaced through ``/health``."""

    degraded: bool = False
    reason: str | None = None

    def set(self, reason: str) -> None:
        self.degraded = True
        self.reason = reason
        logger.warning("Falling back to generated sample data: %s", reason)

    def clear(self) -> None:
        self.degraded = False
        self.reason = None


degraded_state = DegradedState()

_provider: DataProvider | None = None
_lock = threading.Lock()


def _build(settings: Settings, rules: Rules) -> DataProvider:
    """Construct the configured provider, degrading rather than raising."""
    if not settings.settletrace_use_sandbox:
        degraded_state.clear()
        logger.info("Using generated sample-data provider (no network calls)")
        return SampleDataProvider(rules)

    if not settings.razorpay_configured:
        degraded_state.set(
            "SETTLETRACE_USE_SANDBOX is set but Razorpay credentials are "
            "missing. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET to use live "
            "data."
        )
        return SampleDataProvider(rules)

    try:
        from .razorpay_client import RazorpayProvider

        provider = RazorpayProvider(settings)
    except Exception as exc:
        # Covers the razorpay package being absent and the SDK rejecting the
        # credentials at construction. Either way the demo continues.
        degraded_state.set(f"Could not initialise the Razorpay client: {exc}")
        return SampleDataProvider(rules)

    degraded_state.clear()
    logger.info("Using Razorpay sandbox provider")
    return provider


def get_provider(
    settings: Settings | None = None, rules: Rules | None = None
) -> DataProvider:
    """Return the process-wide provider, building it on first use.

    Cached by hand rather than with ``lru_cache``: ``Settings`` is an unhashable
    Pydantic model, so a decorated function raises ``TypeError`` the moment a
    caller passes one - which made the override path used by tests unusable.
    Explicit arguments therefore bypass the cache and build a fresh provider.
    """
    if settings is not None or rules is not None:
        return _build(settings or get_settings(), rules or get_rules())

    global _provider
    if _provider is None:
        with _lock:
            if _provider is None:
                _provider = _build(get_settings(), get_rules())
    return _provider


def reset_provider() -> None:
    """Drop the cached provider. Used by tests and by the demo reset script."""
    global _provider
    with _lock:
        _provider = None
    degraded_state.clear()


# Kept so existing callers written against the lru_cache API keep working.
get_provider.cache_clear = reset_provider  # type: ignore[attr-defined]
