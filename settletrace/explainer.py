"""The AI explanation layer (FR-3.1, FR-3.2).

PRD 5.3 puts AI deliberately outside the matching decision: the reconciliation
outcome is deterministic and auditable, and this layer only translates a finished
outcome into language a merchant can act on, plus an ordering so the largest
discrepancies are reviewed first.

The boundary is structural rather than a convention to be remembered. Nothing
here receives a mutable exception row: ``explain_exception`` takes a plain
read-only summary and returns a string, so there is no object for it to write a
reason code or an amount onto even by mistake. The one caller that persists the
result writes only ``explanation_text``.

Ranking (FR-3.2) is computed arithmetically from the mismatched amount, not by
the model - a ranking the LLM invented would be an AI judgement re-entering the
review path through the back door.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings
from .llm_clients import AuthFailure, build_client
from .models import ExplanationSource, ReasonCode

logger = logging.getLogger(__name__)

# Cap on simultaneous in-flight explanation calls (see ``explain_many``).
MAX_CONCURRENT_EXPLANATIONS = 8

SYSTEM_PROMPT = """You explain payment reconciliation exceptions to merchant \
finance staff who are not engineers.

You are given the result of a reconciliation check that has already been decided \
by a deterministic engine. Your job is only to explain it in plain language.

Rules:
- Two or three sentences. No preamble, no bullet points, no headings.
- Say what the discrepancy is, then the most likely cause, then what the merchant \
should check. Hedge the cause ("most likely", "commonly") - you are inferring it.
- Use rupees, not paise, and write amounts like INR 1,234.56.
- Never state that the discrepancy is resolved, approved, or safe to ignore. \
A human decides that.
- Do not invent transaction IDs, dates, or amounts beyond those given."""

# Fallback text used when the LLM is unavailable. FR-3.1 makes the explanation a
# 'Should', not a 'Must', and an exception with no explanation must still reach
# the reviewer with its numbers intact - so an outage degrades the prose, never
# the exception list itself.
_FALLBACK_TEMPLATES = {
    ReasonCode.FEE_MISMATCH: (
        "The processing fee deducted does not match the fee expected under your "
        "rate card. Check whether your contracted MDR for this payment method has "
        "changed."
    ),
    ReasonCode.GST_MISMATCH: (
        "The GST charged does not match 18% of the processing fee. Check whether "
        "the fee itself was correct before querying the tax."
    ),
    ReasonCode.RESERVE_MISMATCH: (
        "The reserve amount held back differs from your configured reserve rate. "
        "Check your current reserve arrangement."
    ),
    ReasonCode.UNMATCHED_TRANSACTION: (
        "This captured payment does not appear in the settlement being audited. "
        "It may be held for a later payout, or it may have been missed."
    ),
    ReasonCode.LATE_AUTHORIZATION_PENDING: (
        "This payment was authorised but not yet captured when the settlement was "
        "generated, so it is expected in a later payout rather than this one."
    ),
    ReasonCode.AMOUNT_MISMATCH: (
        "The transaction amount does not match the amount settled for it. Check "
        "for a partial refund or capture against this payment."
    ),
    ReasonCode.SETTLEMENT_TOTAL_MISMATCH: (
        "The settlement total does not equal the sum of its verified transactions. "
        "Some component of this payout is unaccounted for."
    ),
}


@dataclass
class LLMHealth:
    """What the LLM has actually done, as opposed to how it is configured.

    A configured key is a claim; this is evidence. The header badge reads from
    here so it cannot advertise "AI: connected" while every explanation on
    screen is really a template - an Explainer is built per batch, so the
    observation has to outlive the instance that made it.
    """

    auth_failed: bool = False
    last_error: str | None = None
    successful_calls: int = 0

    @property
    def is_usable(self) -> bool:
        return not self.auth_failed

    def note_success(self) -> None:
        self.successful_calls += 1
        self.auth_failed = False
        self.last_error = None

    def note_auth_failure(self, detail: str) -> None:
        self.auth_failed = True
        self.last_error = detail

    def reset(self) -> None:
        self.auth_failed = False
        self.last_error = None
        self.successful_calls = 0


llm_health = LLMHealth()


@dataclass(frozen=True)
class ExceptionSummary:
    """A read-only view of an exception, safe to hand to the LLM.

    Frozen and carrying no database identity on purpose: the explanation layer
    is structurally unable to mutate the reconciliation record it describes.
    """

    reason_code: ReasonCode
    expected_paise: int
    actual_paise: int
    delta_paise: int
    transaction_amount_paise: int
    method: str


@dataclass(frozen=True)
class Explanation:
    """Explanatory text plus an honest record of where it came from.

    The two travel together deliberately. Returning a bare string would let a
    caller persist fallback text and label it as AI-generated, which is the one
    thing the disclosure requirement forbids.
    """

    text: str
    source: ExplanationSource

    @property
    def is_ai_generated(self) -> bool:
        return self.source is ExplanationSource.LLM


def format_inr(paise: int) -> str:
    """Render paise as rupees. Merchants read rupees, not minor units.

    Spelled "INR" rather than with the rupee sign because this text travels
    into LLM prompts, CSV exports and log lines, where a non-ASCII glyph is the
    kind of thing that arrives mangled through one encoding hop. The UI swaps
    it for the symbol at render time, so the reader still sees a consistent
    currency beside every other amount on screen.
    """
    return f"INR {paise / 100:,.2f}"


def compute_impact_score(summary: ExceptionSummary) -> int:
    """Rank an exception by revenue at stake, in paise (FR-3.2).

    A fee overcharge risks the delta; an unmatched or pending transaction risks
    the whole amount, which is why the two are scored differently rather than
    both being ranked on ``delta_paise``.
    """
    if summary.reason_code in {
        ReasonCode.UNMATCHED_TRANSACTION,
        ReasonCode.LATE_AUTHORIZATION_PENDING,
    }:
        return abs(summary.transaction_amount_paise)
    return abs(summary.delta_paise)


def fallback_explanation(summary: ExceptionSummary) -> str:
    """Deterministic explanation used when the LLM is unavailable."""
    base = _FALLBACK_TEMPLATES.get(
        summary.reason_code, "This item could not be automatically verified."
    )
    if summary.delta_paise and summary.reason_code not in {
        ReasonCode.UNMATCHED_TRANSACTION,
        ReasonCode.LATE_AUTHORIZATION_PENDING,
    }:
        direction = "more than" if summary.delta_paise > 0 else "less than"
        base += (
            f" The deduction was {format_inr(abs(summary.delta_paise))} "
            f"{direction} expected."
        )
    return base


def _build_prompt(summary: ExceptionSummary) -> str:
    lines = [
        f"Reason code: {summary.reason_code.value}",
        f"Payment method: {summary.method}",
        f"Transaction amount: {format_inr(summary.transaction_amount_paise)}",
    ]
    if summary.reason_code not in {
        ReasonCode.UNMATCHED_TRANSACTION,
        ReasonCode.LATE_AUTHORIZATION_PENDING,
    }:
        lines += [
            f"Expected deduction: {format_inr(summary.expected_paise)}",
            f"Actual deduction: {format_inr(summary.actual_paise)}",
            f"Difference: {format_inr(summary.delta_paise)}",
        ]
    return "\n".join(lines)


class Explainer:
    """Generates plain-language explanations for reconciliation exceptions.

    Falls back to deterministic template text whenever the LLM is unconfigured
    or unreachable, so the dashboard is never blocked on an external service.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Set when the provider rejects the key, so is_live stops claiming a
        # live model after the first authentication failure.
        self._auth_failed = False

        provider = settings.active_llm_provider
        if not settings.llm_configured:
            logger.info(
                "No LLM API key configured; using deterministic fallback "
                "explanations."
            )
            self._client = None
            return

        # build_client never raises: a missing package, an unknown provider
        # name or a malformed key all return None, and None means templates.
        # The constructor must never be what takes the app down.
        self._client = build_client(
            provider, settings.llm_api_key, settings.llm_model_name
        )
        if self._client is not None:
            logger.info(
                "LLM explanations enabled via %s (%s)",
                provider,
                settings.llm_model_name,
            )

    @property
    def provider(self) -> str:
        """Which provider is active, for logging and the credential report."""
        return self._settings.active_llm_provider if self._client else "none"

    @property
    def is_live(self) -> bool:
        """Whether explanations are actually coming from the model.

        Surfaced to the dashboard so a reviewer knows which they are reading.

        A constructed client is not sufficient evidence: the SDK accepts an
        invalid key happily and only fails at call time, so reporting "client
        exists" would badge the header "AI: connected" while every explanation
        on screen was really a template. Once a call has failed
        authentication, this reports False.
        """
        return self._client is not None and not self._auth_failed

    def _note_failure(self, exc: Exception) -> None:
        """Record an authentication failure so ``is_live`` stops claiming live.

        Only auth failures flip the flag. A timeout or a 500 is transient and
        the next call may well succeed, but a rejected key will be rejected
        every time, and the header must not keep advertising a model that is
        not answering.
        """
        # AuthFailure is raised by every client for a rejected key; the string
        # checks stay as a safety net for anything raised below that layer.
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        if (
            isinstance(exc, AuthFailure)
            or "authentication" in name
            or "permission" in name
            or "401" in text
        ):
            self._auth_failed = True
            llm_health.note_auth_failure(f"{type(exc).__name__}: {exc}")

    def _fallback(self, summary: ExceptionSummary) -> Explanation:
        return Explanation(
            text=fallback_explanation(summary), source=ExplanationSource.FALLBACK
        )

    def explain(self, summary: ExceptionSummary) -> Explanation:
        """Return a plain-language explanation. Never raises.

        An explanation is advisory text on an exception that has already been
        recorded, so a failure here must not fail the batch that produced it.
        A failure degrades to template text and says so through the returned
        ``source``, rather than passing the template off as a model response.
        """
        if self._client is None:
            return self._fallback(summary)

        try:
            text = self._client.complete(SYSTEM_PROMPT, _build_prompt(summary))
            if not text:
                return self._fallback(summary)
            llm_health.note_success()
            return Explanation(text=text, source=ExplanationSource.LLM)
        except Exception as exc:
            self._note_failure(exc)
            logger.warning(
                "LLM explanation failed for %s (%s); using template text",
                summary.reason_code.value,
                type(exc).__name__,
            )
            return self._fallback(summary)

    async def explain_async(self, summary: ExceptionSummary) -> Explanation:
        """Async variant of :meth:`explain`, for concurrent batch explanation."""
        if self._client is None:
            return self._fallback(summary)

        try:
            text = await self._client.complete_async(
                SYSTEM_PROMPT, _build_prompt(summary)
            )
            if not text:
                return self._fallback(summary)
            llm_health.note_success()
            return Explanation(text=text, source=ExplanationSource.LLM)
        except Exception as exc:
            self._note_failure(exc)
            logger.warning(
                "Async LLM explanation failed for %s (%s); using template text",
                summary.reason_code.value,
                type(exc).__name__,
            )
            return self._fallback(summary)

    async def explain_many(
        self, summaries: list[ExceptionSummary]
    ) -> list[Explanation]:
        """Explain a batch of exceptions concurrently, bounded.

        Explanations are independent of one another and each is a network
        round-trip, so explaining them in sequence makes a batch of twenty pay
        twenty latencies for no reason. The semaphore keeps a large batch from
        opening hundreds of simultaneous connections and tripping the provider's
        rate limit, which would turn every explanation in the batch into a
        fallback - trading one slow batch for a uniformly degraded one.
        """
        import asyncio

        if self._client is None:
            return [self._fallback(s) for s in summaries]

        limiter = asyncio.Semaphore(MAX_CONCURRENT_EXPLANATIONS)

        async def one(summary: ExceptionSummary) -> Explanation:
            async with limiter:
                return await self.explain_async(summary)

        return list(await asyncio.gather(*(one(s) for s in summaries)))


def rank_exceptions(summaries: list[ExceptionSummary]) -> list[int]:
    """Return 1-based ranks, highest revenue impact first (FR-3.2).

    Ranking only orders the list. FR-3.2's business rule is explicit that it must
    not hide or remove anything, so every input gets a rank.
    """
    scored = sorted(
        range(len(summaries)),
        key=lambda i: compute_impact_score(summaries[i]),
        reverse=True,
    )
    ranks = [0] * len(summaries)
    for rank, index in enumerate(scored, start=1):
        ranks[index] = rank
    return ranks
