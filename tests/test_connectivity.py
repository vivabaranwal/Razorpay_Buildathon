"""Tests for the Razorpay connectivity probe.

These are offline: the network path is exercised manually and in the README's
verified-results section, not on every test run. What is pinned here is that
each failure mode is reported honestly and distinguishably - the probe's whole
purpose is telling apart three situations that look identical from the UI.
"""

from __future__ import annotations

import pytest

from settletrace.config import Settings
from settletrace.providers.connectivity import _classify, probe_razorpay


class TestUnconfigured:
    def test_missing_credentials_is_reported_not_faked(self) -> None:
        """A probe with no keys must never look like a successful call."""
        result = probe_razorpay(Settings(razorpay_key_id="", razorpay_key_secret=""))

        assert result.reachable is False
        assert result.error_kind == "not_configured"
        assert "RAZORPAY_KEY_ID" in result.detail

    def test_half_configured_is_still_unconfigured(self) -> None:
        """A key without its secret cannot authenticate; say so up front."""
        result = probe_razorpay(
            Settings(razorpay_key_id="rzp_test_abc", razorpay_key_secret="")
        )
        assert result.error_kind == "not_configured"


class TestErrorClassification:
    """Each kind maps to a different operator action, so they must not blur."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("Authentication failed", "auth_failed"),
            ("401 Unauthorized", "auth_failed"),
            ("429 Too Many Requests", "rate_limited"),
            ("rate limit exceeded", "rate_limited"),
            ("503 Service Unavailable", "server_error"),
            ("Read timed out", "timeout"),
            ("Connection refused", "unreachable"),
            ("Failed to resolve host", "unreachable"),
            ("something entirely new", "unknown"),
        ],
    )
    def test_classification(self, message: str, expected: str) -> None:
        kind, detail = _classify(Exception(message))
        assert kind == expected
        assert detail

    def test_auth_failure_names_the_variables_to_check(self) -> None:
        """The commonest real failure, so the message must be actionable."""
        _, detail = _classify(Exception("Authentication failed"))
        assert "RAZORPAY_KEY_ID" in detail
        assert "RAZORPAY_KEY_SECRET" in detail


class TestResultShape:
    def test_result_serialises_for_the_api(self) -> None:
        result = probe_razorpay(Settings(razorpay_key_id="", razorpay_key_secret=""))
        payload = result.as_dict()

        assert set(payload) == {
            "reachable",
            "detail",
            "checked_at",
            "latency_ms",
            "endpoint",
            "payments_visible",
            "sample_payment_id",
            "error_kind",
        }

    def test_endpoint_is_read_only(self) -> None:
        """Running the probe against a real account must have no side effect."""
        result = probe_razorpay(Settings(razorpay_key_id="", razorpay_key_secret=""))
        assert result.endpoint.startswith("GET ")
