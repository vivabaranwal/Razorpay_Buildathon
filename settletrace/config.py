"""Configuration loading for SettleTrace.

Two distinct kinds of configuration, deliberately kept apart:

* Secrets and environment wiring (API keys, database URL) come from the
  environment. SRS 4.3.3 requires keys never be hard-coded or logged.
* Reconciliation rules (fee rates, tolerances, backoff schedules, resolution
  windows) come from ``config/rules.yaml``. FRS section 10 requires these live
  in configuration so a rate-card change is not a code change.
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "rules.yaml"


class Settings(BaseSettings):
    """Environment-sourced settings. Never rendered into logs or responses."""

    # The .env path is anchored to the project root, not the working
    # directory. Resolving it relatively means starting uvicorn from anywhere
    # but the project root silently ignores the file - credentials appear
    # unset with no error, which is a miserable thing to debug minutes before
    # a demo. A real environment variable still wins over the file.
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""

    # Which LLM writes the exception explanations. Any of these is optional -
    # with none configured the system uses deterministic template text and
    # says so in the UI. Kept swappable because the explanation layer is
    # advisory by design, so the choice of model is a deployment detail rather
    # than something the reconciliation logic can depend on.
    llm_provider: str = "auto"

    gemini_api_key: str = ""
    groq_api_key: str = ""
    anthropic_api_key: str = ""

    # Empty means "use the sensible default for whichever provider is active",
    # resolved by ``llm_model_name`` below. Set it only to override.
    llm_model: str = ""

    # When false the whole system runs on generated sample data and makes no
    # outbound calls. This is the default so the demo works without credentials.
    settletrace_use_sandbox: bool = False

    database_url: str = "sqlite:///./settletrace.db"

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def active_llm_provider(self) -> str:
        """Which provider to use, resolving ``auto`` against the keys present.

        Groq first, then Gemini, then Anthropic. All three are fine; the order
        reflects free-tier headroom rather than capability. Gemini's free tier
        rate-limits quickly enough that a demo run can exhaust it and fall back
        to templates mid-presentation, which is the one failure worth avoiding
        by default. Either can be forced with LLM_PROVIDER.
        """
        explicit = (self.llm_provider or "auto").strip().lower()
        if explicit != "auto":
            return explicit
        if self.groq_api_key:
            return "groq"
        if self.gemini_api_key:
            return "gemini"
        if self.anthropic_api_key:
            return "anthropic"
        return "none"

    @property
    def llm_api_key(self) -> str:
        """The key belonging to the active provider."""
        return {
            "gemini": self.gemini_api_key,
            "groq": self.groq_api_key,
            "anthropic": self.anthropic_api_key,
        }.get(self.active_llm_provider, "")

    @property
    def llm_model_name(self) -> str:
        """The model to call, defaulted per provider unless overridden."""
        if self.llm_model:
            return self.llm_model
        # Verified against each provider's live API. Model names are retired
        # faster than anything else here - the previous defaults returned 404
        # with a perfectly valid key, which reads as "the key is broken"
        # unless you look at the response body. If one 404s, list the
        # provider's current models rather than guessing a successor.
        return {
            "gemini": "gemini-3.6-flash",
            "groq": "openai/gpt-oss-120b",
            "anthropic": "claude-sonnet-5",
        }.get(self.active_llm_provider, "")

    @property
    def llm_configured(self) -> bool:
        """Whether any provider has a key. Explanations degrade if not."""
        return bool(self.llm_api_key)


class Rules(BaseModel):
    """Reconciliation rules loaded from ``config/rules.yaml``."""

    fee_rates: dict[str, dict[str, float]]
    gst_rate: float
    reserve_rates: dict[str, float]
    match_tolerance_paise: int
    fee_tolerance_paise: int
    resolution_windows_seconds: dict[str, int]
    backoff_schedule_seconds: list[int]
    backoff_max_attempts: int
    webhook_event_retention_hours: int = Field(default=24)

    def fee_rate_for(self, method: str, merchant: str = "default") -> float:
        """MDR fraction for a payment method, falling back to the default card.

        An unknown method must not silently become a zero fee - that would make
        a real deduction look like an overcharge. Falling back to the merchant's
        card rate keeps the comparison meaningful, and FR-1.5 will flag the
        resulting delta for a human rather than hiding it.
        """
        table = self.fee_rates.get(merchant) or self.fee_rates["default"]
        if method in table:
            return table[method]
        return table.get("card", 0.0)

    def reserve_rate_for(self, merchant: str = "default") -> float:
        return self.reserve_rates.get(merchant, self.reserve_rates["default"])

    def resolution_window_for(self, method: str) -> int:
        """Seconds a payment may stay non-terminal before it is stuck (FR-2.1)."""
        return self.resolution_windows_seconds.get(
            method, self.resolution_windows_seconds["default"]
        )

    def backoff_delay_for(self, attempt: int) -> int:
        """Delay before re-check number ``attempt`` (0-indexed), capped (FR-2.2)."""
        schedule = self.backoff_schedule_seconds
        if attempt >= len(schedule):
            return schedule[-1]
        return schedule[attempt]


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@functools.lru_cache(maxsize=1)
def get_rules(path: Path | None = None) -> Rules:
    rules_path = path or DEFAULT_RULES_PATH
    with open(rules_path, "r", encoding="utf-8") as handle:
        return Rules(**yaml.safe_load(handle))
