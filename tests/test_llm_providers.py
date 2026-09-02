"""Tests for the swappable LLM provider layer.

Offline: no test here makes a network call. What is pinned is that provider
selection resolves correctly, that every failure degrades to template text
rather than raising, and that the disclosure fields stay honest whichever
provider is active.
"""

from __future__ import annotations

import pytest

from settletrace.config import Settings
from settletrace.explainer import ExceptionSummary, Explainer
from settletrace.llm_clients import (
    AuthFailure,
    GeminiClient,
    GroqClient,
    build_client,
)
from settletrace.models import ExplanationSource, ReasonCode


def _summary() -> ExceptionSummary:
    return ExceptionSummary(
        reason_code=ReasonCode.FEE_MISMATCH,
        expected_paise=2_000,
        actual_paise=2_500,
        delta_paise=500,
        transaction_amount_paise=100_000,
        method="card",
    )


class TestProviderSelection:
    def test_auto_prefers_groq(self) -> None:
        """Ordered by free-tier headroom, not by model capability.

        Gemini's free tier rate-limits quickly enough that a demo run can
        exhaust it and drop to templates mid-presentation.
        """
        settings = Settings(gemini_api_key="g", groq_api_key="q", anthropic_api_key="a")
        assert settings.active_llm_provider == "groq"

    def test_auto_falls_through_to_gemini_then_anthropic(self) -> None:
        assert Settings(gemini_api_key="g", anthropic_api_key="a").active_llm_provider == "gemini"
        assert Settings(anthropic_api_key="a").active_llm_provider == "anthropic"

    def test_no_keys_resolves_to_none(self) -> None:
        assert Settings().active_llm_provider == "none"
        assert Settings().llm_configured is False

    def test_explicit_provider_overrides_auto(self) -> None:
        settings = Settings(
            llm_provider="groq", gemini_api_key="g", groq_api_key="q"
        )
        assert settings.active_llm_provider == "groq"
        assert settings.llm_api_key == "q"

    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("gemini", "gemini-3.6-flash"),
            ("groq", "openai/gpt-oss-120b"),
            ("anthropic", "claude-sonnet-5"),
        ],
    )
    def test_each_provider_has_its_own_default_model(
        self, provider: str, expected: str
    ) -> None:
        """A model name from one provider is meaningless to another, so the
        default has to follow the provider rather than being global."""
        settings = Settings(llm_provider=provider, **{f"{provider}_api_key": "k"})
        assert settings.llm_model_name == expected

    def test_explicit_model_overrides_the_default(self) -> None:
        settings = Settings(
            llm_provider="gemini", gemini_api_key="k", llm_model="gemini-1.5-pro"
        )
        assert settings.llm_model_name == "gemini-1.5-pro"


class TestClientConstruction:
    def test_no_key_builds_nothing(self) -> None:
        assert build_client("gemini", "", "gemini-3.6-flash") is None

    def test_unknown_provider_returns_none_rather_than_raising(self) -> None:
        """A typo in LLM_PROVIDER must degrade, not crash the app."""
        assert build_client("gpt-9", "key", "model") is None

    def test_known_providers_build(self) -> None:
        assert isinstance(build_client("gemini", "k", "m"), GeminiClient)
        assert isinstance(build_client("groq", "k", "m"), GroqClient)


class TestRequestShapes:
    """Pins the wire format each provider expects; they differ meaningfully."""

    def test_gemini_sends_system_prompt_as_its_own_field(self) -> None:
        payload = GeminiClient("k", "gemini-3.6-flash")._payload("SYS", "USER")

        assert payload["system_instruction"]["parts"][0]["text"] == "SYS"
        assert payload["contents"][0]["parts"][0]["text"] == "USER"

    def test_groq_sends_system_prompt_as_a_message_role(self) -> None:
        payload = GroqClient("k", "openai/gpt-oss-120b")._payload("SYS", "USER")

        assert payload["messages"][0] == {"role": "system", "content": "SYS"}
        assert payload["messages"][1] == {"role": "user", "content": "USER"}

    def test_gemini_key_travels_in_a_header_not_the_url(self) -> None:
        """A URL ends up in proxy logs and error messages; a secret must not."""
        client = GeminiClient("SECRET_KEY", "gemini-3.6-flash")

        assert "SECRET_KEY" not in client._url
        assert client._headers()["x-goog-api-key"] == "SECRET_KEY"

    def test_groq_uses_bearer_auth(self) -> None:
        client = GroqClient("SECRET_KEY", "m")
        assert client._headers()["Authorization"] == "Bearer SECRET_KEY"

    def test_empty_response_extracts_as_none(self) -> None:
        """An empty completion must read as failure, not as an empty
        explanation persisted under an AI label."""
        assert GeminiClient._extract({"candidates": []}) is None
        assert GroqClient._extract({"choices": []}) is None


class TestDegradation:
    """The behaviour that matters tonight: never crash, never mislabel."""

    def test_no_key_falls_back(self) -> None:
        explainer = Explainer(Settings())
        result = explainer.explain(_summary())

        assert explainer.is_live is False
        assert result.source is ExplanationSource.FALLBACK
        assert result.text

    def test_unknown_provider_falls_back(self) -> None:
        explainer = Explainer(
            Settings(llm_provider="nonsense", gemini_api_key="k")
        )
        assert explainer.explain(_summary()).source is ExplanationSource.FALLBACK

    def test_call_failure_falls_back_and_downgrades_liveness(self) -> None:
        """A rejected key must stop the header claiming a live model."""

        class Rejecting:
            name, model = "gemini", "gemini-3.6-flash"

            def complete(self, system: str, user: str) -> str | None:
                raise AuthFailure("Gemini rejected the key (400)")

        explainer = Explainer(Settings(gemini_api_key="bad"))
        explainer._client = Rejecting()

        result = explainer.explain(_summary())
        assert result.source is ExplanationSource.FALLBACK
        assert explainer.is_live is False

    def test_transient_failure_does_not_downgrade_liveness(self) -> None:
        """A timeout may well succeed next call; only auth failures are
        permanent until the key changes."""

        class Timing:
            name, model = "gemini", "gemini-3.6-flash"

            def complete(self, system: str, user: str) -> str | None:
                raise TimeoutError("read timed out")

        explainer = Explainer(Settings(gemini_api_key="k"))
        explainer._client = Timing()

        assert explainer.explain(_summary()).source is ExplanationSource.FALLBACK
        assert explainer.is_live is True

    def test_successful_call_is_labelled_as_llm(self) -> None:
        class Working:
            name, model = "gemini", "gemini-3.6-flash"

            def complete(self, system: str, user: str) -> str | None:
                return "The fee charged was higher than your rate card allows."

        explainer = Explainer(Settings(gemini_api_key="k"))
        explainer._client = Working()

        result = explainer.explain(_summary())
        assert result.source is ExplanationSource.LLM
        assert result.is_ai_generated is True

    def test_empty_completion_is_labelled_fallback_not_llm(self) -> None:
        """Otherwise an empty string would be persisted as AI-written text."""

        class Empty:
            name, model = "gemini", "gemini-3.6-flash"

            def complete(self, system: str, user: str) -> str | None:
                return None

        explainer = Explainer(Settings(gemini_api_key="k"))
        explainer._client = Empty()

        assert explainer.explain(_summary()).source is ExplanationSource.FALLBACK
