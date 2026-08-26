"""
Comprehensive tests for MeterService.
Validates Idempotency (Cases A, B, C), Concurrent Same-Key Deduplication,
Transaction Atomicity, and Error Handling.
"""
from __future__ import annotations

import concurrent.futures
import uuid
from datetime import date
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.domain.exceptions import (
    IdempotencyConflictError,
    InvalidUsageError,
    QuotaExceededError,
)
from app.domain.token_usage import TokenUsage
from app.models.quota_counter import QuotaCounter
from app.models.tenant import Tenant
from app.models.usage_event import UsageEvent
from app.services.meter_service import MeterService


class TestMeterServiceIdempotency:
    """Tests Idempotency Cases A, B, C for MeterService."""

    @pytest.fixture
    def service(self) -> MeterService:
        return MeterService()

    def test_case_a_first_request_creates_event_and_deducts_quota(
        self, db_session: Session, service: MeterService, tenant_a: Tenant, current_period: date
    ):
        """Case A: First request with key 'k1' and payload X."""
        payload = {"model": "gemini-pro", "prompt": "Hello world"}
        key = "idemp-test-001"

        res = service.record_usage(
            session=db_session,
            tenant_id=tenant_a.id,
            usage_type="api_call",
            quantity=10,
            idempotency_key=key,
            request_payload=payload,
            billing_period_start=current_period,
        )

        assert res["quantity"] == 10
        assert res["cost_micro_cents"] == 1000  # 10 * 100 micro-cents
        assert res["quota_used"] == 10
        assert res["is_idempotent_replay"] is False

        # Verify usage_events row in DB
        event = db_session.execute(
            select(UsageEvent).where(
                UsageEvent.tenant_id == tenant_a.id,
                UsageEvent.idempotency_key == key,
            )
        ).scalars().first()
        assert event is not None
        assert event.quantity == 10
        assert event.payload_hash == service.compute_payload_hash(payload)
        assert event.response_payload["id"] == res["id"]

    def test_case_b_exact_retry_returns_original_response_without_recharging(
        self, db_session: Session, service: MeterService, tenant_a: Tenant, current_period: date
    ):
        """Case B: Exact retry with same key 'k1' and same payload X."""
        payload = {"model": "gemini-pro", "prompt": "Hello world"}
        key = "idemp-test-002"

        # Request 1
        res1 = service.record_usage(
            session=db_session,
            tenant_id=tenant_a.id,
            usage_type="api_call",
            quantity=15,
            idempotency_key=key,
            request_payload=payload,
            billing_period_start=current_period,
        )
        db_session.flush()

        # Request 2 (Retry)
        res2 = service.record_usage(
            session=db_session,
            tenant_id=tenant_a.id,
            usage_type="api_call",
            quantity=15,
            idempotency_key=key,
            request_payload=payload,
            billing_period_start=current_period,
        )

        # Responses must match
        assert res2["id"] == res1["id"]
        assert res2["quantity"] == 15
        assert res2["cost_micro_cents"] == res1["cost_micro_cents"]

        # Check that quota was only deducted ONCE (15, not 30)
        counter = db_session.execute(
            select(QuotaCounter.used).where(
                QuotaCounter.tenant_id == tenant_a.id,
                QuotaCounter.usage_type == "api_call",
                QuotaCounter.billing_period_start == current_period,
            )
        ).scalar()
        assert counter == 15

        # Check that exactly ONE event exists
        events_count = db_session.execute(
            text("SELECT COUNT(*) FROM usage_events WHERE tenant_id=:tid AND idempotency_key=:key"),
            {"tid": str(tenant_a.id), "key": key},
        ).scalar()
        assert events_count == 1

    def test_case_c_same_key_different_payload_raises_conflict(
        self, db_session: Session, service: MeterService, tenant_a: Tenant, current_period: date
    ):
        """Case C: Same key 'k1' but different payload Y."""
        key = "idemp-test-003"
        payload_1 = {"model": "gemini-pro", "prompt": "Original prompt"}
        payload_2 = {"model": "gemini-pro", "prompt": "TAMPERED prompt"}

        service.record_usage(
            session=db_session,
            tenant_id=tenant_a.id,
            usage_type="api_call",
            quantity=5,
            idempotency_key=key,
            request_payload=payload_1,
            billing_period_start=current_period,
        )
        db_session.flush()

        # Second request with different payload must fail with IdempotencyConflictError
        with pytest.raises(IdempotencyConflictError, match="already used with a different request payload"):
            service.record_usage(
                session=db_session,
                tenant_id=tenant_a.id,
                usage_type="api_call",
                quantity=5,
                idempotency_key=key,
                request_payload=payload_2,
                billing_period_start=current_period,
            )

        # Quota must not have been increased for the rejected payload
        counter = db_session.execute(
            select(QuotaCounter.used).where(
                QuotaCounter.tenant_id == tenant_a.id,
                QuotaCounter.usage_type == "api_call",
                QuotaCounter.billing_period_start == current_period,
            )
        ).scalar()
        assert counter == 5

    def test_token_usage_recording_with_breakdown(
        self, db_session: Session, service: MeterService, tenant_a: Tenant, current_period: date
    ):
        token_usage = TokenUsage(
            input_tokens=1000,
            output_tokens=500,
            cached_input_tokens=200,
            reasoning_tokens=100,
        )
        payload = {"prompt_tokens": 1000, "completion_tokens": 500}

        res = service.record_usage(
            session=db_session,
            tenant_id=tenant_a.id,
            usage_type="token",
            quantity=1500,
            idempotency_key="token-event-001",
            request_payload=payload,
            token_usage=token_usage,
            billing_period_start=current_period,
        )

        assert res["quantity"] == 1500
        # Cost: 800 uncached * 150 + 200 cached * 75 + 500 output * 600
        # = 120,000 + 15,000 + 300,000 = 435,000 micro-cents ($0.000435)
        assert res["cost_micro_cents"] == 435_000


class TestMeterServiceValidationAndAtomicity:
    """Tests validation constraints and transactional rollback guarantees."""

    @pytest.fixture
    def service(self) -> MeterService:
        return MeterService()

    def test_invalid_quantity_rejected(self, db_session: Session, service: MeterService, tenant_a: Tenant):
        with pytest.raises(InvalidUsageError, match="quantity must be between 1 and 1,000,000"):
            service.record_usage(
                session=db_session,
                tenant_id=tenant_a.id,
                usage_type="api_call",
                quantity=0,
                idempotency_key="bad-qty-0",
                request_payload={},
            )

        with pytest.raises(InvalidUsageError, match="quantity must be between 1 and 1,000,000"):
            service.record_usage(
                session=db_session,
                tenant_id=tenant_a.id,
                usage_type="api_call",
                quantity=1_000_001,
                idempotency_key="bad-qty-over",
                request_payload={},
            )

    def test_empty_idempotency_key_rejected(self, db_session: Session, service: MeterService, tenant_a: Tenant):
        with pytest.raises(InvalidUsageError, match="idempotency_key must be a non-empty string"):
            service.record_usage(
                session=db_session,
                tenant_id=tenant_a.id,
                usage_type="api_call",
                quantity=1,
                idempotency_key="   ",
                request_payload={},
            )

    def test_token_quantity_mismatch_rejected(self, db_session: Session, service: MeterService, tenant_a: Tenant):
        token_usage = TokenUsage(input_tokens=100, output_tokens=50)  # total 150
        with pytest.raises(InvalidUsageError, match="TokenUsage total .* does not match declared quantity"):
            service.record_usage(
                session=db_session,
                tenant_id=tenant_a.id,
                usage_type="token",
                quantity=200,  # Mismatch
                idempotency_key="mismatch-tok-qty",
                request_payload={},
                token_usage=token_usage,
            )

    def test_quota_exceeded_leaves_no_usage_event(
        self, db_session: Session, service: MeterService, tenant_a: Tenant, current_period: date
    ):
        # Set limit to 10
        service.quota_enforcer.ensure_quota_counter(
            db_session, tenant_a.id, "api_call", current_period, plan_limit=10
        )

        with pytest.raises(QuotaExceededError):
            service.record_usage(
                session=db_session,
                tenant_id=tenant_a.id,
                usage_type="api_call",
                quantity=11,  # Exceeds limit
                idempotency_key="over-limit-key",
                request_payload={},
                billing_period_start=current_period,
            )

        # Verify no usage event was recorded
        count = db_session.execute(
            text("SELECT COUNT(*) FROM usage_events WHERE tenant_id=:tid AND idempotency_key='over-limit-key'"),
            {"tid": str(tenant_a.id)},
        ).scalar()
        assert count == 0


class TestMeterServiceConcurrencyPostgreSQL:
    """
    Live multi-threaded PostgreSQL tests for MeterService.
    Proves that simultaneous duplicate requests on the same key do NOT cause 500 errors
    and do NOT double-charge or create multiple usage events.
    """

    def test_concurrent_same_key_requests_exactly_once(self, db_engine):
        SessionLocal = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
        setup_session = SessionLocal()

        tenant = Tenant(
            id=uuid.uuid4(),
            name="Concurrent Meter Tenant",
            email=f"meter-conc-{uuid.uuid4().hex[:8]}@example.com",
        )
        setup_session.add(tenant)
        setup_session.commit()

        tenant_id = tenant.id
        period = date(2026, 8, 1)
        service = MeterService()
        service.quota_enforcer.ensure_quota_counter(
            setup_session, tenant_id, "api_call", period, plan_limit=1000
        )
        setup_session.commit()
        setup_session.close()

        key = f"concurrent-same-key-{uuid.uuid4().hex}"
        payload = {"action": "generate_text", "prompt": "What is 2+2?"}

        def worker_request():
            thread_session = SessionLocal()
            try:
                res = service.record_usage(
                    session=thread_session,
                    tenant_id=tenant_id,
                    usage_type="api_call",
                    quantity=10,
                    idempotency_key=key,
                    request_payload=payload,
                    billing_period_start=period,
                )
                thread_session.commit()
                return {"success": True, "data": res}
            except Exception as e:
                thread_session.rollback()
                return {"success": False, "error": str(e)}
            finally:
                thread_session.close()

        # Run 8 concurrent threads with the exact same idempotency key and payload
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(worker_request) for _ in range(8)]
            results = [f.result() for f in futures]

        # ALL 8 threads must succeed without HTTP 500 or uncaught errors!
        for idx, res in enumerate(results):
            assert res["success"] is True, f"Thread {idx} failed: {res.get('error')}"

        # All threads must receive the same event ID
        event_ids = {r["data"]["id"] for r in results}
        assert len(event_ids) == 1, f"Expected exactly 1 event ID across all threads, got {event_ids}"

        # Verify DB state: exactly 1 usage event recorded
        verify_session = SessionLocal()
        event_count = verify_session.execute(
            text("SELECT COUNT(*) FROM usage_events WHERE tenant_id=:tid AND idempotency_key=:key"),
            {"tid": str(tenant_id), "key": key},
        ).scalar()
        assert event_count == 1

        # Verify DB state: quota used is 10 (not 80!)
        counter = verify_session.execute(
            select(QuotaCounter.used).where(
                QuotaCounter.tenant_id == tenant_id,
                QuotaCounter.usage_type == "api_call",
                QuotaCounter.billing_period_start == period,
            )
        ).scalar()
        assert counter == 10
        verify_session.close()

    def test_forced_failure_before_commit_rolls_back_both_quota_and_event(self, db_engine):
        """
        Adversarial test: If an unhandled application failure or crash happens
        after record_usage but before session commit, both quota and event must rollback.
        """
        SessionLocal = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
        setup_session = SessionLocal()

        tenant = Tenant(
            id=uuid.uuid4(),
            name="Crash Test Tenant",
            email=f"crash-{uuid.uuid4().hex[:8]}@example.com",
        )
        setup_session.add(tenant)
        setup_session.commit()
        tenant_id = tenant.id
        period = date(2026, 9, 1)

        service = MeterService()
        service.quota_enforcer.ensure_quota_counter(
            setup_session, tenant_id, "api_call", period, plan_limit=1000
        )
        setup_session.commit()
        setup_session.close()

        # Execute transaction that fails/aborts before commit
        tx_session = SessionLocal()
        service.record_usage(
            session=tx_session,
            tenant_id=tenant_id,
            usage_type="api_call",
            quantity=50,
            idempotency_key="crash-key-001",
            request_payload={"action": "test"},
            billing_period_start=period,
        )
        # Simulate unhandled exception / crash before commit
        tx_session.rollback()
        tx_session.close()

        # Verify: zero quota consumed, zero events stored
        verify_session = SessionLocal()
        counter = verify_session.execute(
            select(QuotaCounter.used).where(
                QuotaCounter.tenant_id == tenant_id,
                QuotaCounter.usage_type == "api_call",
                QuotaCounter.billing_period_start == period,
            )
        ).scalar()
        assert counter == 0, f"Expected used=0 after rollback, got {counter}"

        event_count = verify_session.execute(
            text("SELECT COUNT(*) FROM usage_events WHERE tenant_id=:tid"),
            {"tid": str(tenant_id)},
        ).scalar()
        assert event_count == 0, f"Expected 0 events after rollback, got {event_count}"
        verify_session.close()

    def test_quota_initialization_race_under_concurrency(self, db_engine):
        """
        Adversarial test: Multiple concurrent workers initialize quota for a new tenant/month.
        Ensures exactly 1 counter row exists and all usage is accounted for without overwriting.
        """
        SessionLocal = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
        setup_session = SessionLocal()

        tenant = Tenant(
            id=uuid.uuid4(),
            name="Init Race Tenant",
            email=f"init-race-{uuid.uuid4().hex[:8]}@example.com",
        )
        setup_session.add(tenant)
        setup_session.commit()
        tenant_id = tenant.id
        setup_session.close()

        period = date(2026, 10, 1)
        service = MeterService()

        def worker_init_and_deduct(worker_id: int):
            thread_session = SessionLocal()
            try:
                service.record_usage(
                    session=thread_session,
                    tenant_id=tenant_id,
                    usage_type="api_call",
                    quantity=10,
                    idempotency_key=f"init-worker-key-{worker_id}",
                    request_payload={"worker": worker_id},
                    billing_period_start=period,
                )
                thread_session.commit()
                return True
            except Exception as e:
                thread_session.rollback()
                return False
            finally:
                thread_session.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker_init_and_deduct, i) for i in range(5)]
            results = [f.result() for f in futures]

        assert all(results), "All concurrent initialization requests should succeed"

        verify_session = SessionLocal()
        counter_rows = verify_session.execute(
            select(QuotaCounter).where(
                QuotaCounter.tenant_id == tenant_id,
                QuotaCounter.usage_type == "api_call",
                QuotaCounter.billing_period_start == period,
            )
        ).scalars().all()

        assert len(counter_rows) == 1, "Exactly one quota_counter row must be created"
        assert counter_rows[0].used == 50, f"Expected 50 used (5*10), got {counter_rows[0].used}"
        verify_session.close()
