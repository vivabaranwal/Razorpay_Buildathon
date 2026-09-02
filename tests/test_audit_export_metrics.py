"""Tests for the audit trail, batch export, and detection metrics."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from settletrace.audit import (
    ENTITY_EXCEPTION,
    ENTITY_ORDER,
    read_trail,
    record_change,
    render_value,
)
from settletrace.batch_service import resolve_exception, run_settlement_batch
from settletrace.config import Rules, Settings
from settletrace.engine.metrics import DetectionMetrics, evaluate_detection
from settletrace.export import export_csv, export_filename, export_json
from settletrace.models import AuditLog, Exception_, PaymentStatus, ReasonCode
from settletrace.providers.sample import SampleDataProvider
from settletrace.reconciler_service import recheck_order

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def _settings() -> Settings:
    return Settings(anthropic_api_key="", settletrace_use_sandbox=False)


class TestRenderValue:
    def test_enum_renders_as_its_value(self) -> None:
        """The trail reads as 'captured', not 'PaymentStatus.CAPTURED'."""
        assert render_value(PaymentStatus.CAPTURED) == "captured"

    def test_bool_renders_lowercase(self) -> None:
        assert render_value(True) == "true"
        assert render_value(False) == "false"

    def test_none_stays_none(self) -> None:
        assert render_value(None) is None

    def test_overlong_value_is_truncated_not_raised(self) -> None:
        """Losing the audit row would be worse than losing a value's tail."""
        rendered = render_value("x" * 500)
        assert rendered is not None
        assert len(rendered) <= 255


class TestAuditTrail:
    def test_change_is_recorded(self, session) -> None:
        record_change(
            session,
            entity_type=ENTITY_ORDER,
            entity_id="order_1",
            field_changed="local_status",
            old_value=PaymentStatus.PENDING,
            new_value=PaymentStatus.CAPTURED,
            reason="ground-truth poll disagreed",
        )
        session.flush()

        rows = session.scalars(select(AuditLog)).all()
        assert len(rows) == 1
        assert rows[0].old_value == "pending"
        assert rows[0].new_value == "captured"
        assert rows[0].changed_by == "system"

    def test_trail_reads_newest_first(self, session) -> None:
        for i in range(3):
            record_change(
                session,
                entity_type=ENTITY_ORDER,
                entity_id=f"order_{i}",
                field_changed="local_status",
                old_value="pending",
                new_value="captured",
            )
        session.flush()

        trail = read_trail(session)
        assert [r.entity_id for r in trail] == ["order_2", "order_1", "order_0"]

    def test_trail_filters_by_entity(self, session) -> None:
        record_change(
            session,
            entity_type=ENTITY_ORDER,
            entity_id="order_1",
            field_changed="local_status",
            old_value="pending",
            new_value="captured",
        )
        record_change(
            session,
            entity_type=ENTITY_EXCEPTION,
            entity_id="7",
            field_changed="resolved_flag",
            old_value=False,
            new_value=True,
        )
        session.flush()

        assert len(read_trail(session, entity_type=ENTITY_ORDER)) == 1
        assert len(read_trail(session, entity_type=ENTITY_EXCEPTION)) == 1
        assert len(read_trail(session)) == 2

    def test_ordering_survives_identical_timestamps(self, session) -> None:
        """Rows written in one transaction can share a microsecond.

        Ordering by id rather than by changed_at is what keeps the true
        sequence readable when several corrections commit together.
        """
        stamp = NOW
        for i in range(3):
            record_change(
                session,
                entity_type=ENTITY_ORDER,
                entity_id=f"order_{i}",
                field_changed="local_status",
                old_value="pending",
                new_value="captured",
                changed_at=stamp,
            )
        session.flush()

        trail = read_trail(session)
        assert [r.entity_id for r in trail] == ["order_2", "order_1", "order_0"]


class TestAuditIntegration:
    """The trail must capture changes made through the real code paths."""

    def test_status_correction_writes_a_trail_row(self, session, rules: Rules) -> None:
        """FR-2.4: a correction and its audit row are inseparable."""
        from settletrace.models import Order

        order = Order(
            order_id="order_A",
            payment_id="pay_STUCK000",
            amount_paise=49_900,
            method="upi",
            local_status=PaymentStatus.PENDING,
            created_at=NOW - timedelta(hours=2),
            updated_at=NOW - timedelta(hours=2),
        )
        session.add(order)
        session.flush()

        provider = SampleDataProvider(rules)
        recheck_order(session, order, provider, rules, NOW)
        session.flush()

        trail = read_trail(session, entity_type=ENTITY_ORDER, entity_id="order_A")
        assert len(trail) == 1
        assert trail[0].field_changed == "local_status"
        assert trail[0].old_value == "pending"
        assert trail[0].new_value == "captured"
        assert trail[0].changed_by == "system"

    def test_unchanged_status_writes_no_trail_row(self, session, rules: Rules) -> None:
        """The trail records changes, not every poll that found nothing."""
        from settletrace.models import Order

        order = Order(
            order_id="order_B",
            payment_id="pay_UNKNOWN",  # sample provider reports 'pending'
            amount_paise=49_900,
            method="upi",
            local_status=PaymentStatus.PENDING,
            created_at=NOW - timedelta(hours=2),
            updated_at=NOW - timedelta(hours=2),
        )
        session.add(order)
        session.flush()

        recheck_order(session, order, SampleDataProvider(rules), rules, NOW)
        session.flush()

        assert read_trail(session, entity_type=ENTITY_ORDER) == []

    def test_resolving_an_exception_names_the_human(
        self, session, rules: Rules
    ) -> None:
        """An audit row is only useful if it says who signed the item off."""
        provider = SampleDataProvider(rules)
        provider.generate("setl_AUDIT", n_transactions=50, defect_rate=0.20)
        run_settlement_batch(session, "setl_AUDIT", provider, rules, _settings())

        row = session.scalars(select(Exception_)).first()
        assert row is not None
        resolve_exception(session, row.id, resolved_by="finance_lead")
        session.flush()

        trail = read_trail(session, entity_type=ENTITY_EXCEPTION)
        assert len(trail) == 1
        assert trail[0].changed_by == "finance_lead"
        assert trail[0].new_value == "true"

    def test_resolving_twice_records_one_change(self, session, rules: Rules) -> None:
        """Re-resolving is not a state change and must not pad the trail."""
        provider = SampleDataProvider(rules)
        provider.generate("setl_AUDIT", n_transactions=50, defect_rate=0.20)
        run_settlement_batch(session, "setl_AUDIT", provider, rules, _settings())

        row = session.scalars(select(Exception_)).first()
        assert row is not None
        resolve_exception(session, row.id, resolved_by="finance_lead")
        resolve_exception(session, row.id, resolved_by="someone_else")
        session.flush()

        assert len(read_trail(session, entity_type=ENTITY_EXCEPTION)) == 1


class TestDetectionMetrics:
    def test_perfect_detection(self) -> None:
        truth = [("pay_1", ReasonCode.FEE_MISMATCH)]
        assert evaluate_detection(truth, truth).is_perfect

    def test_missed_defect_lowers_recall_only(self) -> None:
        injected = [
            ("pay_1", ReasonCode.FEE_MISMATCH),
            ("pay_2", ReasonCode.GST_MISMATCH),
        ]
        detected = [("pay_1", ReasonCode.FEE_MISMATCH)]
        m = evaluate_detection(detected, injected)

        assert m.precision == 1.0
        assert m.recall == 0.5
        assert m.false_negatives == 1

    def test_false_alarm_lowers_precision_only(self) -> None:
        injected = [("pay_1", ReasonCode.FEE_MISMATCH)]
        detected = [
            ("pay_1", ReasonCode.FEE_MISMATCH),
            ("pay_9", ReasonCode.FEE_MISMATCH),
        ]
        m = evaluate_detection(detected, injected)

        assert m.precision == 0.5
        assert m.recall == 1.0
        assert m.false_positives == 1

    def test_right_transaction_wrong_reason_is_not_a_hit(self) -> None:
        """Flagging a fee dispute on a missing payment helps nobody.

        Scoring per transaction alone would call this a detection and overstate
        what the engine actually told the merchant.
        """
        injected = [("pay_1", ReasonCode.UNMATCHED_TRANSACTION)]
        detected = [("pay_1", ReasonCode.FEE_MISMATCH)]
        m = evaluate_detection(detected, injected)

        assert m.true_positives == 0
        assert m.false_positives == 1
        assert m.false_negatives == 1

    def test_flagging_everything_is_caught_by_precision(self) -> None:
        """The failure mode a bare accuracy percentage cannot see."""
        injected = [("pay_1", ReasonCode.FEE_MISMATCH)]
        detected = [(f"pay_{i}", ReasonCode.FEE_MISMATCH) for i in range(1, 21)]
        m = evaluate_detection(detected, injected)

        assert m.recall == 1.0  # found the real one
        assert m.precision == 0.05  # ...along with 19 false alarms
        assert m.f1 < 0.11

    def test_settlement_total_finding_is_not_scored(self) -> None:
        """It is a consequence of planted defects, not a planted fault itself."""
        injected = [("pay_1", ReasonCode.FEE_MISMATCH)]
        detected = [
            ("pay_1", ReasonCode.FEE_MISMATCH),
            (None, ReasonCode.SETTLEMENT_TOTAL_MISMATCH),
        ]
        assert evaluate_detection(detected, injected).is_perfect

    def test_empty_batch_does_not_divide_by_zero(self) -> None:
        m = evaluate_detection([], [])
        assert m.precision == 1.0
        assert m.recall == 1.0

    def test_duplicate_findings_are_collapsed(self) -> None:
        """One transaction can raise the same finding twice; score it once."""
        injected = [("pay_1", ReasonCode.FEE_MISMATCH)]
        detected = [
            ("pay_1", ReasonCode.FEE_MISMATCH),
            ("pay_1", ReasonCode.FEE_MISMATCH),
        ]
        assert evaluate_detection(detected, injected).true_positives == 1


class TestExport:
    def _batch(self, session, rules: Rules):
        provider = SampleDataProvider(rules)
        provider.generate("setl_EXPORT", n_transactions=80, defect_rate=0.12)
        return run_settlement_batch(
            session, "setl_EXPORT", provider, rules, _settings(), is_labeled=True
        )

    def test_json_carries_summary_and_exceptions(self, session, rules: Rules) -> None:
        batch = self._batch(session, rules)
        payload = json.loads(export_json(session, batch))

        assert payload["summary"]["batch_id"] == batch.id
        assert payload["summary"]["transactions_processed"] == 80

        # Every exception the batch stored appears in the file - compared
        # against the table rather than against the batch's transaction counter,
        # which counts excepted transactions, not exception rows (one
        # transaction can raise several, and the settlement-total finding
        # belongs to no transaction at all).
        stored = session.scalars(
            select(Exception_).where(Exception_.batch_id == batch.id)
        ).all()
        assert len(payload["exceptions"]) == len(stored)

    def test_csv_parses_and_has_every_exception(self, session, rules: Rules) -> None:
        batch = self._batch(session, rules)
        text = export_csv(session, batch)

        # Strip the leading comment lines; the remainder must be valid CSV.
        body = "\n".join(
            line for line in text.splitlines() if not line.startswith("#")
        )
        rows = list(csv.DictReader(io.StringIO(body)))

        stored = session.scalars(
            select(Exception_).where(Exception_.batch_id == batch.id)
        ).all()
        assert len(rows) == len(stored)
        assert all(r["reason_code"] for r in rows)

    def test_export_states_its_provenance(self, session, rules: Rules) -> None:
        """The artifact outlives the app, so it must carry its own caveats."""
        batch = self._batch(session, rules)

        summary = json.loads(export_json(session, batch))["summary"]
        assert summary["accuracy_is_measured"] is True
        assert "labelled sample" in summary["data_provenance"].lower()

        assert "data_provenance" in export_csv(session, batch)

    def test_export_records_explanation_source_per_row(
        self, session, rules: Rules
    ) -> None:
        """A reader must be able to tell model prose from a template."""
        batch = self._batch(session, rules)
        rows = json.loads(export_json(session, batch))["exceptions"]

        assert rows
        assert all(r["explanation_source"] in {"llm", "fallback"} for r in rows)

    def test_export_includes_resolved_exceptions(self, session, rules: Rules) -> None:
        """A report omitting resolved items would misstate what was found."""
        batch = self._batch(session, rules)
        row = session.scalars(
            select(Exception_).where(Exception_.batch_id == batch.id)
        ).first()
        assert row is not None
        resolve_exception(session, row.id, resolved_by="finance_lead")
        session.flush()

        rows = json.loads(export_json(session, batch))["exceptions"]
        assert any(r["resolved"] == "yes" for r in rows)

    def test_amounts_export_unformatted_for_spreadsheets(
        self, session, rules: Rules
    ) -> None:
        """A formatted string would import as text, not a summable number."""
        batch = self._batch(session, rules)
        rows = json.loads(export_json(session, batch))["exceptions"]

        for row in rows:
            assert "," not in row["delta_inr"]
            assert "₹" not in row["delta_inr"]
            float(row["delta_inr"])  # raises if not parseable

    def test_filename_identifies_the_batch(self, session, rules: Rules) -> None:
        batch = self._batch(session, rules)
        name = export_filename(batch, "csv")

        assert name.endswith(".csv")
        assert f"batch{batch.id}" in name
        assert "setl_EXPORT" in name
