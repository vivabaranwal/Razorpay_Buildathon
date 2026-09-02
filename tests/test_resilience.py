"""Credential and failure-path tests.

These pin the behaviour that matters when something is misconfigured on stage:
the app degrades visibly instead of crashing, and never claims a capability it
does not have.
"""

from __future__ import annotations

from settletrace.config import PROJECT_ROOT, Settings
from settletrace.explainer import (
    ExceptionSummary,
    Explainer,
    LLMHealth,
)
from settletrace.models import ExplanationSource, ReasonCode
from settletrace.providers.factory import degraded_state, get_provider, reset_provider
from settletrace.providers.sample import SampleDataProvider
from settletrace.startup import credential_report, mask


def _summary() -> ExceptionSummary:
    return ExceptionSummary(
        reason_code=ReasonCode.FEE_MISMATCH,
        expected_paise=2_000,
        actual_paise=2_500,
        delta_paise=500,
        transaction_amount_paise=100_000,
        method="card",
    )


class TestSandboxDegradation:
    """Missing or broken Razorpay credentials must not take the app down."""

    def teardown_method(self) -> None:
        reset_provider()

    def test_sandbox_without_credentials_degrades_not_raises(self, rules) -> None:
        """The exact state that used to return a 500 from every endpoint."""
        reset_provider()
        provider = get_provider(
            Settings(
                settletrace_use_sandbox=True,
                razorpay_key_id="",
                razorpay_key_secret="",
            ),
            rules,
        )

        assert isinstance(provider, SampleDataProvider)
        assert degraded_state.degraded is True
        assert "RAZORPAY_KEY_ID" in (degraded_state.reason or "")

    def test_normal_sample_mode_is_not_degraded(self, rules) -> None:
        reset_provider()
        provider = get_provider(Settings(settletrace_use_sandbox=False), rules)

        assert isinstance(provider, SampleDataProvider)
        assert degraded_state.degraded is False

    def test_provider_accepts_explicit_settings(self, rules) -> None:
        """Guards a regression: lru_cache made any argument raise TypeError.

        Settings is an unhashable Pydantic model, so the decorated function
        blew up the moment a caller passed one - which is how the API's own
        override path was reached.
        """
        assert get_provider(Settings(), rules) is not None


class TestLLMDegradation:
    """A missing, malformed or rejected key degrades to templates, silently
    to the user but never dishonestly."""

    def test_no_key_is_not_live(self) -> None:
        explainer = Explainer(Settings(anthropic_api_key=""))
        assert explainer.is_live is False

        result = explainer.explain(_summary())
        assert result.source is ExplanationSource.FALLBACK
        assert result.text

    def test_malformed_key_does_not_crash_construction(self) -> None:
        """The constructor must never be what takes the app down."""
        explainer = Explainer(Settings(anthropic_api_key="not-a-real-key"))
        result = explainer.explain(_summary())
        assert result.source is ExplanationSource.FALLBACK

    def test_health_stops_claiming_live_after_auth_failure(self) -> None:
        """A configured key is a claim; a successful call is evidence.

        The SDK accepts an invalid key at construction and only fails when
        called, so reporting "client exists" would badge the header
        "AI: connected" while every explanation on screen was a template.
        """
        health = LLMHealth()
        assert health.is_usable is True

        health.note_auth_failure("AuthenticationError: 401")
        assert health.is_usable is False

        health.note_success()
        assert health.is_usable is True


class TestCredentialReport:
    def test_secrets_are_never_printed_in_full(self) -> None:
        """The report goes to a console that is often being screen-shared."""
        secret = "sk-ant-super-secret-value-that-must-not-leak"
        rendered = mask(secret)

        assert secret not in rendered
        assert rendered.startswith("sk-ant-sup")

    def test_missing_values_are_named_as_missing(self) -> None:
        assert mask("") == "(missing)"

    def test_report_names_every_expected_variable(self) -> None:
        lines = "\n".join(credential_report(Settings()))

        for variable in (
            "RAZORPAY_KEY_ID",
            "RAZORPAY_KEY_SECRET",
            "ANTHROPIC_API_KEY",
            "SETTLETRACE_USE_SANDBOX",
        ):
            assert variable in lines

    def test_report_flags_sandbox_on_without_keys(self) -> None:
        """The most confusing misconfiguration, so it is called out by name."""
        lines = "\n".join(
            credential_report(
                Settings(
                    settletrace_use_sandbox=True,
                    razorpay_key_id="",
                    razorpay_key_secret="",
                )
            )
        )
        assert "Sandbox mode is ON but Razorpay credentials are missing" in lines
        assert "GENERATED data" in lines

    def test_report_flags_keys_present_but_sandbox_off(self) -> None:
        lines = "\n".join(
            credential_report(
                Settings(
                    settletrace_use_sandbox=False,
                    razorpay_key_id="rzp_test_abc123",
                    razorpay_key_secret="secret",
                )
            )
        )
        assert "sandbox mode is OFF" in lines


class TestEnvFileAnchoring:
    def test_env_path_is_anchored_to_the_project_root(self) -> None:
        """Otherwise the file is silently ignored unless the server is started
        from the project root - an awful thing to debug before a demo."""
        env_file = Settings.model_config.get("env_file")
        assert env_file == PROJECT_ROOT / ".env"
