"""API tests covering the endpoints in FRS section 9."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from settletrace.api.app import app
from settletrace.db import get_session
from settletrace.models import Base, Order, PaymentStatus


@pytest.fixture
def client(rules) -> Iterator[TestClient]:
    """A client bound to an isolated in-memory database.

    The provider cache is cleared per test so each one gets a fresh sample
    generator rather than inheriting another test's generated batch.
    """
    # StaticPool keeps one shared connection so the in-memory database survives
    # across the request thread TestClient runs endpoints on; check_same_thread
    # lets that thread use it. This mirrors the connect_args in db.py.
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_session():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    from settletrace.providers.factory import get_provider

    get_provider.cache_clear()
    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_provider.cache_clear()
        engine.dispose()


class TestHealth:
    def test_health_reports_data_source_and_llm_mode(self, client) -> None:
        """A reviewer must be able to tell generated data from sandbox data."""
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["data_source"] in {"generated_sample", "razorpay_sandbox"}
        assert body["llm_explanations"] in {"live", "fallback_templates"}


class TestSettlementBatch:
    def test_trigger_batch_returns_summary(self, client) -> None:
        """FR-1.6 / FR-4.1: throughput and accuracy come back on the batch."""
        response = client.post(
            "/batches/settlement", json={"settlement_id": "setl_API", "explain": True}
        )
        assert response.status_code == 201

        body = response.json()
        assert body["transactions_processed"] > 0
        assert body["accuracy_pct"] is not None
        assert (
            body["transactions_verified"] + body["transactions_exception"]
            == body["transactions_processed"]
        )

    def test_accuracy_is_flagged_as_measured_on_labelled_data(self, client) -> None:
        """FR-1.6 business rule: only a labelled batch may claim a measurement."""
        body = client.post("/batches/settlement", json={"settlement_id": "s1"}).json()
        assert body["accuracy_is_measured"] is True

    def test_summary_is_retrievable_by_id(self, client) -> None:
        created = client.post("/batches/settlement", json={"settlement_id": "s1"})
        batch_id = created.json()["id"]

        fetched = client.get(f"/batches/{batch_id}/summary")
        assert fetched.status_code == 200
        assert fetched.json()["id"] == batch_id

    def test_unknown_batch_returns_404(self, client) -> None:
        assert client.get("/batches/9999/summary").status_code == 404


class TestExceptionList:
    @pytest.fixture
    def batch_id(self, client) -> int:
        response = client.post(
            "/batches/settlement", json={"settlement_id": "setl_EXC"}
        )
        return response.json()["id"]

    def test_exceptions_are_ranked_by_impact(self, client, batch_id) -> None:
        """FR-3.2 acceptance: the largest discrepancy is shown first."""
        rows = client.get("/exceptions", params={"batch_id": batch_id}).json()
        assert rows

        ranks = [r["impact_rank"] for r in rows]
        assert ranks == sorted(ranks)
        impacts = [r["impact_score"] for r in rows]
        assert impacts == sorted(impacts, reverse=True)

    def test_every_exception_carries_an_explanation(self, client, batch_id) -> None:
        """FR-3.1: explanation text is attached to each exception."""
        rows = client.get("/exceptions", params={"batch_id": batch_id}).json()
        assert all(r["explanation_text"] for r in rows)

    def test_filter_by_reason_code(self, client, batch_id) -> None:
        """FR-4.2 acceptance: only exceptions with that code are shown."""
        all_rows = client.get("/exceptions", params={"batch_id": batch_id}).json()
        target = all_rows[0]["reason_code"]

        filtered = client.get(
            "/exceptions", params={"batch_id": batch_id, "reason_code": target}
        ).json()

        assert filtered
        assert all(r["reason_code"] == target for r in filtered)
        assert len(filtered) <= len(all_rows)

    def test_clearing_the_filter_restores_the_full_list(
        self, client, batch_id
    ) -> None:
        """FR-4.2 business rule: filtering narrows the view, not the data."""
        before = client.get("/exceptions", params={"batch_id": batch_id}).json()
        client.get(
            "/exceptions",
            params={"batch_id": batch_id, "reason_code": "fee_mismatch"},
        )
        after = client.get("/exceptions", params={"batch_id": batch_id}).json()

        assert len(after) == len(before)

    def test_search_by_transaction_id(self, client, batch_id) -> None:
        rows = client.get("/exceptions", params={"batch_id": batch_id}).json()
        target = next(r["transaction_id"] for r in rows if r["transaction_id"])

        found = client.get(
            "/exceptions", params={"batch_id": batch_id, "search": target}
        ).json()

        assert [r["transaction_id"] for r in found] == [target]

    def test_resolving_hides_from_the_open_list_only(self, client, batch_id) -> None:
        """UC-2: a resolved exception is closed, never deleted."""
        rows = client.get("/exceptions", params={"batch_id": batch_id}).json()
        target = rows[0]["id"]

        resolved = client.post(
            f"/exceptions/{target}/resolve", json={"resolved_by": "finance_lead"}
        )
        assert resolved.status_code == 200
        assert resolved.json()["resolved_flag"] is True

        open_rows = client.get("/exceptions", params={"batch_id": batch_id}).json()
        assert target not in [r["id"] for r in open_rows]

        with_resolved = client.get(
            "/exceptions", params={"batch_id": batch_id, "include_resolved": True}
        ).json()
        assert target in [r["id"] for r in with_resolved]

    def test_resolving_unknown_exception_returns_404(self, client) -> None:
        assert client.post("/exceptions/9999/resolve", json={}).status_code == 404


class TestStuckOrders:
    def _seed(self, client, age_seconds: int, order_id: str = "order_X") -> None:
        """Insert an order directly through the overridden session."""
        session_gen = app.dependency_overrides[get_session]()
        session = next(session_gen)
        session.add(
            Order(
                order_id=order_id,
                payment_id="pay_STUCK000",
                amount_paise=49_900,
                method="upi",
                local_status=PaymentStatus.PENDING,
                created_at=datetime.now(timezone.utc)
                - timedelta(seconds=age_seconds),
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        session.close()

    def test_stale_order_appears_as_stuck(self, client) -> None:
        """FR-2.1 acceptance: past its window, an open order is a candidate."""
        self._seed(client, age_seconds=7200)
        rows = client.get("/orders/stuck").json()
        assert [r["order_id"] for r in rows] == ["order_X"]

    def test_fresh_order_is_not_stuck(self, client) -> None:
        self._seed(client, age_seconds=30)
        assert client.get("/orders/stuck").json() == []

    def test_recheck_corrects_a_desynced_order(self, client) -> None:
        """FR-2.3 / FR-2.4: poll ground truth and correct the local record.

        The sample provider reports pay_STUCK000 as captured - the PRD 2.2
        scenario where the payment succeeded but the webhook never arrived.
        """
        self._seed(client, age_seconds=7200)
        # Prime the provider so it knows the ground truth for this payment.
        from settletrace.providers.factory import get_provider

        get_provider().generate_stuck_orders()

        body = client.post("/orders/order_X/recheck").json()
        assert body["previous_status"] == "pending"
        assert body["actual_status"] == "captured"
        assert body["corrected"] is True

    def test_recheck_unknown_order_returns_404(self, client) -> None:
        assert client.post("/orders/nope/recheck").status_code == 404


class TestWebhooks:
    def _payload(self, payment_id: str = "pay_1") -> dict:
        return {
            "event": "payment.captured",
            "payload": {"payment": {"entity": {"id": payment_id}}},
        }

    def test_first_delivery_is_processed(self, client) -> None:
        response = client.post(
            "/webhooks/razorpay",
            json=self._payload(),
            headers={"x-razorpay-event-id": "evt_001"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "event_id": "evt_001",
            "processed": True,
            "duplicate": False,
        }

    def test_duplicate_delivery_is_discarded_with_200(self, client) -> None:
        """FR-2.5 acceptance: the same event twice yields one update.

        The duplicate is acknowledged rather than rejected - a non-2xx would
        make Razorpay retry a message that was already handled correctly.
        """
        headers = {"x-razorpay-event-id": "evt_dup"}
        client.post("/webhooks/razorpay", json=self._payload(), headers=headers)
        second = client.post(
            "/webhooks/razorpay", json=self._payload(), headers=headers
        )

        assert second.status_code == 200
        assert second.json()["duplicate"] is True
        assert second.json()["processed"] is False

    def test_missing_event_id_header_is_rejected(self, client) -> None:
        """Without the idempotency key there is no way to deduplicate."""
        response = client.post("/webhooks/razorpay", json=self._payload())
        assert response.status_code == 422


class TestClientContract:
    """Guards the field names the TypeScript client reads.

    A renamed or mistyped field does not fail loudly in the browser - it reads
    as `undefined`, which is falsy, so a disclosure flag silently flips to its
    safe-looking default. That is exactly how an "AI-generated" badge could end
    up on template text, so the names the client depends on are pinned here.
    """

    def test_exception_exposes_the_disclosure_fields(self, client) -> None:
        client.post("/batches/settlement", json={"settlement_id": "setl_CONTRACT"})
        rows = client.get("/exceptions").json()
        assert rows

        required = {
            "id",
            "reason_code",
            "transaction_id",
            "settlement_id",
            "expected_paise",
            "actual_paise",
            "delta_paise",
            "impact_score",
            "impact_rank",
            "explanation_text",
            "explanation_source",
            "is_ai_explained",
            "resolved_flag",
            "resolved_by",
        }
        missing = required - set(rows[0])
        assert not missing, f"API no longer sends: {missing}"

    def test_ai_flag_agrees_with_its_source(self, client) -> None:
        """The boolean and the enum must never disagree about provenance."""
        client.post("/batches/settlement", json={"settlement_id": "setl_CONTRACT"})
        for row in client.get("/exceptions").json():
            assert row["is_ai_explained"] == (row["explanation_source"] == "llm")

    def test_batch_summary_exposes_precision_and_recall(self, client) -> None:
        body = client.post(
            "/batches/settlement", json={"settlement_id": "setl_CONTRACT"}
        ).json()
        required = {
            "accuracy_pct",
            "accuracy_is_measured",
            "precision_pct",
            "recall_pct",
            "f1_pct",
            "true_positives",
            "false_positives",
            "false_negatives",
        }
        assert not required - set(body)
