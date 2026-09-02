"""End-to-end batch tests, and the AI-boundary guarantees (FR-3.1, FR-3.2)."""

from __future__ import annotations

from sqlalchemy import select

from settletrace.batch_service import resolve_exception, run_settlement_batch
from settletrace.config import Rules, Settings
from settletrace.explainer import (
    ExceptionSummary,
    Explainer,
    compute_impact_score,
    format_inr,
    rank_exceptions,
)
from settletrace.models import (
    Exception_,
    ExplanationSource,
    Match,
    MatchStatus,
    ReasonCode,
)
from settletrace.providers.sample import SampleDataProvider


def _settings() -> Settings:
    # No API key, so the explainer uses deterministic fallback text and the
    # tests make no network calls.
    return Settings(anthropic_api_key="", settletrace_use_sandbox=False)


def _summary(
    reason: ReasonCode = ReasonCode.FEE_MISMATCH,
    delta: int = 500,
    amount: int = 100_000,
) -> ExceptionSummary:
    return ExceptionSummary(
        reason_code=reason,
        expected_paise=2_000,
        actual_paise=2_000 + delta,
        delta_paise=delta,
        transaction_amount_paise=amount,
        method="card",
    )


class TestImpactRanking:
    def test_larger_mismatch_ranks_first(self) -> None:
        """FR-3.2 acceptance: the larger of two exceptions appears first."""
        small = _summary(delta=100)
        large = _summary(delta=9_000)
        assert rank_exceptions([small, large]) == [2, 1]

    def test_unmatched_transaction_scores_on_full_amount(self) -> None:
        """The whole payment is at risk, not just a fee delta."""
        unmatched = _summary(
            reason=ReasonCode.UNMATCHED_TRANSACTION, delta=0, amount=500_000
        )
        assert compute_impact_score(unmatched) == 500_000

    def test_every_exception_receives_a_rank(self) -> None:
        """FR-3.2 business rule: ranking hides nothing."""
        summaries = [_summary(delta=d) for d in (100, 5_000, 50)]
        ranks = rank_exceptions(summaries)
        assert sorted(ranks) == [1, 2, 3]


class TestExplainer:
    def test_falls_back_when_no_api_key(self) -> None:
        """An LLM outage must degrade prose, never the exception list."""
        explainer = Explainer(_settings())
        assert explainer.is_live is False

        result = explainer.explain(_summary())
        assert "processing fee" in result.text.lower()
        assert "INR 5.00" in result.text  # the 500-paise delta, as rupees

    def test_fallback_is_labelled_as_fallback(self) -> None:
        """Template text must never be reported as a model response.

        The UI labels an explanation from this field alone, so a fallback
        mislabelled here would put an "AI-generated" badge on a canned string -
        the one disclosure failure the design forbids outright.
        """
        explainer = Explainer(_settings())
        result = explainer.explain(_summary())

        assert result.source is ExplanationSource.FALLBACK
        assert result.is_ai_generated is False

    def test_fallback_never_claims_resolution(self) -> None:
        explainer = Explainer(_settings())
        for reason in ReasonCode:
            text = explainer.explain(_summary(reason=reason)).text.lower()
            assert "resolved" not in text
            assert "safe to ignore" not in text

    def test_amounts_render_as_rupees(self) -> None:
        assert format_inr(123_456) == "INR 1,234.56"
        assert format_inr(100) == "INR 1.00"

    def test_summary_is_immutable(self) -> None:
        """The AI layer is handed nothing it could write a decision onto."""
        import dataclasses
        import pytest

        summary = _summary()
        with pytest.raises(dataclasses.FrozenInstanceError):
            summary.delta_paise = 999  # type: ignore[misc]


class TestEndToEndBatch:
    def test_batch_runs_and_reports(self, session, rules: Rules) -> None:
        """UC-1: a full reconciliation batch, as the demo runs it."""
        provider = SampleDataProvider(rules)
        provider.generate("setl_TEST", n_transactions=100, defect_rate=0.05)

        batch = run_settlement_batch(
            session, "setl_TEST", provider, rules, _settings(), is_labeled=True
        )

        assert batch.transactions_processed == 100
        assert batch.completed_at is not None
        assert 0.0 < (batch.accuracy or 0) <= 1.0

    def test_every_transaction_is_accounted_for(self, session, rules: Rules) -> None:
        """SRS 4.3.2, end to end: verified + exception covers the input."""
        provider = SampleDataProvider(rules)
        provider.generate("setl_TEST", n_transactions=80, defect_rate=0.10)

        batch = run_settlement_batch(
            session, "setl_TEST", provider, rules, _settings()
        )

        assert (
            batch.transactions_verified + batch.transactions_exception
            == batch.transactions_processed
        )

    def test_injected_defects_are_all_detected(self, session, rules: Rules) -> None:
        """FR-1.6 acceptance: measured accuracy against a labelled batch.

        Every planted defect must appear in the exception list. This is the
        claim the PRD's >=95% accuracy target rests on, so it is checked against
        the generator's own ground truth rather than asserted.
        """
        provider = SampleDataProvider(rules)
        sample = provider.generate("setl_TEST", n_transactions=200, defect_rate=0.06)

        run_settlement_batch(session, "setl_TEST", provider, rules, _settings())

        flagged = {
            row.transaction_id
            for row in session.scalars(select(Exception_))
            if row.transaction_id
        }
        planted = {d.transaction_id for d in sample.injected_defects}

        assert planted, "generator should have planted at least one defect"
        assert planted <= flagged, f"missed defects: {planted - flagged}"

    def test_exceptions_are_explained_and_ranked(
        self, session, rules: Rules
    ) -> None:
        """FR-3.1/FR-3.2: text is attached and the list is ordered."""
        provider = SampleDataProvider(rules)
        provider.generate("setl_TEST", n_transactions=60, defect_rate=0.15)

        run_settlement_batch(session, "setl_TEST", provider, rules, _settings())

        rows = session.scalars(select(Exception_)).all()
        assert rows
        assert all(r.explanation_text for r in rows)
        assert all(r.impact_rank is not None for r in rows)

    def test_explanation_does_not_alter_exception_data(
        self, session, rules: Rules
    ) -> None:
        """FR-3.1 acceptance and the core PRD 5.3 guarantee.

        The same batch is reconciled with explanations on and off; the reason
        codes and every numeric field must be byte-identical.
        """
        provider = SampleDataProvider(rules)
        provider.generate("setl_TEST", n_transactions=60, defect_rate=0.15)

        run_settlement_batch(
            session, "setl_TEST", provider, rules, _settings(), explain=False
        )
        without = _exception_fingerprint(session)

        # Reset and re-run the identical batch, this time with explanations.
        for row in session.scalars(select(Exception_)).all():
            session.delete(row)
        for row in session.scalars(select(Match)).all():
            session.delete(row)
        session.flush()

        run_settlement_batch(
            session, "setl_TEST", provider, rules, _settings(), explain=True
        )
        with_explanations = _exception_fingerprint(session)

        assert with_explanations == without

    def test_repeat_ingestion_updates_rather_than_duplicates(
        self, session, rules: Rules
    ) -> None:
        """FR-1.1 business rule: one settlement, one record."""
        from settletrace.models import Settlement

        provider = SampleDataProvider(rules)
        provider.generate("setl_TEST", n_transactions=20)

        run_settlement_batch(session, "setl_TEST", provider, rules, _settings())
        run_settlement_batch(session, "setl_TEST", provider, rules, _settings())

        settlements = session.scalars(select(Settlement)).all()
        assert len(settlements) == 1

    def test_resolution_requires_a_human(self, session, rules: Rules) -> None:
        """FR-1.5 business rule: nothing is auto-discarded."""
        provider = SampleDataProvider(rules)
        provider.generate("setl_TEST", n_transactions=50, defect_rate=0.20)
        run_settlement_batch(session, "setl_TEST", provider, rules, _settings())

        row = session.scalars(select(Exception_)).first()
        assert row is not None
        assert row.resolved_flag is False

        resolved = resolve_exception(session, row.id, resolved_by="finance_lead")
        assert resolved is not None
        assert resolved.resolved_flag is True
        assert resolved.resolved_by == "finance_lead"


def _exception_fingerprint(session) -> set[tuple]:
    """Every field of every exception except the AI-written text."""
    return {
        (
            r.reason_code,
            r.transaction_id,
            r.settlement_id,
            r.expected_paise,
            r.actual_paise,
            r.delta_paise,
            r.impact_score,
            r.impact_rank,
            r.resolved_flag,
        )
        for r in session.scalars(select(Exception_)).all()
    }
