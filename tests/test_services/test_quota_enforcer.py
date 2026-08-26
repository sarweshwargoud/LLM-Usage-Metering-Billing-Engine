"""
Tests for QuotaEnforcer service, including live PostgreSQL concurrency race verification.
"""
from __future__ import annotations

import concurrent.futures
import uuid
from datetime import date
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.domain.exceptions import QuotaExceededError, TenantNotFoundError
from app.models.plan import Plan
from app.models.quota_counter import QuotaCounter
from app.models.tenant import Tenant
from app.services.quota_enforcer import QuotaEnforcer


class TestQuotaEnforcerUnit:
    """Unit and transaction tests for QuotaEnforcer."""

    @pytest.fixture
    def enforcer(self) -> QuotaEnforcer:
        return QuotaEnforcer()

    def test_unknown_tenant_raises_tenant_not_found(self, db_session: Session, enforcer: QuotaEnforcer):
        fake_id = uuid.uuid4()
        with pytest.raises(TenantNotFoundError):
            enforcer.check_and_deduct(db_session, fake_id, "api_call", 1)

    def test_auto_initialization_of_quota_counter(
        self, db_session: Session, enforcer: QuotaEnforcer, tenant_a: Tenant, current_period: date
    ):
        result = enforcer.check_and_deduct(
            db_session, tenant_a.id, "api_call", 5, billing_period_start=current_period
        )
        assert result["used"] == 5
        assert result["plan_limit"] == 1000  # Default Free plan api_call_limit

        counter = db_session.execute(
            select(QuotaCounter).where(
                QuotaCounter.tenant_id == tenant_a.id,
                QuotaCounter.usage_type == "api_call",
                QuotaCounter.billing_period_start == current_period,
            )
        ).scalars().first()
        assert counter is not None
        assert counter.used == 5
        assert counter.plan_limit == 1000

    def test_exact_quota_boundary_success(
        self, db_session: Session, enforcer: QuotaEnforcer, tenant_a: Tenant, current_period: date
    ):
        # Initialize counter at 990 / 1000
        enforcer.ensure_quota_counter(
            db_session, tenant_a.id, "api_call", current_period, plan_limit=1000
        )
        db_session.execute(
            text("UPDATE quota_counters SET used = 990 WHERE tenant_id=:tid AND usage_type='api_call'"),
            {"tid": str(tenant_a.id)},
        )

        # Request exactly remaining 10 units -> should succeed and reach 1000
        res = enforcer.check_and_deduct(
            db_session, tenant_a.id, "api_call", 10, billing_period_start=current_period
        )
        assert res["used"] == 1000

    def test_exceeding_quota_raises_quota_exceeded_error(
        self, db_session: Session, enforcer: QuotaEnforcer, tenant_a: Tenant, current_period: date
    ):
        # Set at exact limit 1000 / 1000
        enforcer.ensure_quota_counter(
            db_session, tenant_a.id, "api_call", current_period, plan_limit=1000
        )
        db_session.execute(
            text("UPDATE quota_counters SET used = 1000 WHERE tenant_id=:tid AND usage_type='api_call'"),
            {"tid": str(tenant_a.id)},
        )

        # Request 1 unit -> must fail
        with pytest.raises(QuotaExceededError) as exc_info:
            enforcer.check_and_deduct(
                db_session, tenant_a.id, "api_call", 1, billing_period_start=current_period
            )
        assert exc_info.value.detail["current_used"] == 1000
        assert exc_info.value.detail["requested_quantity"] == 1

    def test_near_boundary_partial_overrun_rejected(
        self, db_session: Session, enforcer: QuotaEnforcer, tenant_a: Tenant, current_period: date
    ):
        # Set at 990 / 1000 (10 remaining)
        enforcer.ensure_quota_counter(
            db_session, tenant_a.id, "api_call", current_period, plan_limit=1000
        )
        db_session.execute(
            text("UPDATE quota_counters SET used = 990 WHERE tenant_id=:tid AND usage_type='api_call'"),
            {"tid": str(tenant_a.id)},
        )

        # Request 11 units -> exceeds remaining 10 -> must fail
        with pytest.raises(QuotaExceededError) as exc_info:
            enforcer.check_and_deduct(
                db_session, tenant_a.id, "api_call", 11, billing_period_start=current_period
            )
        assert exc_info.value.detail["current_used"] == 990
        assert exc_info.value.detail["plan_limit"] == 1000
        assert exc_info.value.detail["requested_quantity"] == 11

    def test_tenant_isolation(
        self, db_session: Session, enforcer: QuotaEnforcer, tenant_a: Tenant, tenant_b: Tenant, current_period: date
    ):
        # Deduct 500 for tenant A
        enforcer.check_and_deduct(db_session, tenant_a.id, "api_call", 500, current_period)

        # Deduct 200 for tenant B
        enforcer.check_and_deduct(db_session, tenant_b.id, "api_call", 200, current_period)

        counter_a = db_session.execute(
            select(QuotaCounter.used).where(
                QuotaCounter.tenant_id == tenant_a.id,
                QuotaCounter.usage_type == "api_call",
                QuotaCounter.billing_period_start == current_period,
            )
        ).scalar()
        counter_b = db_session.execute(
            select(QuotaCounter.used).where(
                QuotaCounter.tenant_id == tenant_b.id,
                QuotaCounter.usage_type == "api_call",
                QuotaCounter.billing_period_start == current_period,
            )
        ).scalar()

        assert counter_a == 500
        assert counter_b == 200

    def test_period_isolation(
        self, db_session: Session, enforcer: QuotaEnforcer, tenant_a: Tenant
    ):
        period1 = date(2026, 1, 1)
        period2 = date(2026, 2, 1)

        enforcer.check_and_deduct(db_session, tenant_a.id, "api_call", 300, period1)
        enforcer.check_and_deduct(db_session, tenant_a.id, "api_call", 400, period2)

        c1 = db_session.execute(
            select(QuotaCounter.used).where(
                QuotaCounter.tenant_id == tenant_a.id,
                QuotaCounter.usage_type == "api_call",
                QuotaCounter.billing_period_start == period1,
            )
        ).scalar()
        c2 = db_session.execute(
            select(QuotaCounter.used).where(
                QuotaCounter.tenant_id == tenant_a.id,
                QuotaCounter.usage_type == "api_call",
                QuotaCounter.billing_period_start == period2,
            )
        ).scalar()

        assert c1 == 300
        assert c2 == 400


class TestQuotaConcurrencyPostgreSQL:
    """
    Live multi-threaded PostgreSQL concurrency race tests.
    Proves TOCTOU elimination under simultaneous competing worker connections.
    """

    def test_concurrent_quota_requests_never_overrun_limit(self, db_engine):
        """
        Scenario: Limit is 100.
        10 threads simultaneously attempt to consume 20 units each (total 200).
        Exactly 5 threads must succeed (20 * 5 = 100).
        Exactly 5 threads must be rejected with QuotaExceededError.
        Final database used counter must equal 100, NEVER > 100.
        """
        SessionLocal = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
        setup_session = SessionLocal()

        tenant = Tenant(
            id=uuid.uuid4(),
            name="Concurrency Test Tenant",
            email=f"concurrency-{uuid.uuid4().hex[:8]}@example.com",
        )
        setup_session.add(tenant)
        setup_session.flush()

        tenant_id = tenant.id
        period = date(2026, 7, 1)
        enforcer = QuotaEnforcer()
        enforcer.ensure_quota_counter(
            setup_session, tenant_id, "api_call", period, plan_limit=100
        )
        setup_session.commit()
        setup_session.close()

        def worker_attempt():
            thread_session = SessionLocal()
            try:
                enforcer.check_and_deduct(
                    thread_session,
                    tenant_id,
                    "api_call",
                    20,
                    billing_period_start=period,
                )
                thread_session.commit()
                return True
            except QuotaExceededError:
                thread_session.rollback()
                return False
            finally:
                thread_session.close()

        # Launch 10 simultaneous threads
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker_attempt) for _ in range(10)]
            results = [f.result() for f in futures]

        success_count = sum(1 for r in results if r is True)
        failure_count = sum(1 for r in results if r is False)

        assert success_count == 5, f"Expected exactly 5 successes, got {success_count}"
        assert failure_count == 5, f"Expected exactly 5 failures, got {failure_count}"

        verify_session = SessionLocal()
        final_counter = verify_session.execute(
            select(QuotaCounter).where(
                QuotaCounter.tenant_id == tenant_id,
                QuotaCounter.usage_type == "api_call",
                QuotaCounter.billing_period_start == period,
            )
        ).scalars().first()

        assert final_counter.used == 100
        assert final_counter.used <= final_counter.plan_limit
        verify_session.close()
