"""
Tests for Monthly Usage Rollup background aggregation service.
"""
from __future__ import annotations

import uuid
from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.monthly_rollup import MonthlyUsageRollup
from app.models.tenant import Tenant
from app.models.usage_event import UsageEvent
from app.services.rollup_service import RollupService


class TestMonthlyRollups:
    """Tests periodic rollup computation and idempotent upsert behavior."""

    def test_monthly_rollup_aggregates_usage_events_correctly(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        period = date(2026, 3, 1)
        rollup_service = RollupService()

        # Seed usage events for tenant_a (2 token events, 1 api_call event)
        ev1 = UsageEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            idempotency_key="ev1",
            usage_type="token",
            quantity=1000,
            cost_micro_cents=150_000,
            billing_period_start=period,
            payload_hash="h1",
            response_payload={"result": "ok"},
        )
        ev2 = UsageEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            idempotency_key="ev2",
            usage_type="token",
            quantity=2000,
            cost_micro_cents=300_000,
            billing_period_start=period,
            payload_hash="h2",
            response_payload={"result": "ok"},
        )
        ev3 = UsageEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            idempotency_key="ev3",
            usage_type="api_call",
            quantity=10,
            cost_micro_cents=1_000,
            billing_period_start=period,
            payload_hash="h3",
            response_payload={"result": "ok"},
        )

        # Seed usage event for tenant_b
        ev4 = UsageEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            idempotency_key="ev4",
            usage_type="token",
            quantity=500,
            cost_micro_cents=75_000,
            billing_period_start=period,
            payload_hash="h4",
            response_payload={"result": "ok"},
        )

        db_session.add_all([ev1, ev2, ev3, ev4])
        db_session.commit()

        # ── Execution 1: Compute rollups ──────────────────────────────────────
        rows_affected = rollup_service.compute_monthly_rollups(db_session, period)
        assert rows_affected == 3  # (tenant_a, token), (tenant_a, api_call), (tenant_b, token)

        # Verify tenant_a token rollup
        rollup_a_token = db_session.execute(
            select(MonthlyUsageRollup).where(
                MonthlyUsageRollup.tenant_id == tenant_a.id,
                MonthlyUsageRollup.billing_period_start == period,
                MonthlyUsageRollup.usage_type == "token",
            )
        ).scalars().first()
        assert rollup_a_token is not None
        assert rollup_a_token.total_quantity == 3000
        assert rollup_a_token.total_cost_micro_cents == 450_000

        # Verify tenant_a api_call rollup
        rollup_a_api = db_session.execute(
            select(MonthlyUsageRollup).where(
                MonthlyUsageRollup.tenant_id == tenant_a.id,
                MonthlyUsageRollup.billing_period_start == period,
                MonthlyUsageRollup.usage_type == "api_call",
            )
        ).scalars().first()
        assert rollup_a_api is not None
        assert rollup_a_api.total_quantity == 10
        assert rollup_a_api.total_cost_micro_cents == 1_000

        # ── Execution 2: Re-run rollups (Idempotency test) ────────────────────
        rows_affected_2 = rollup_service.compute_monthly_rollups(db_session, period)
        assert rows_affected_2 == 3

        # Confirm totals did NOT double
        db_session.refresh(rollup_a_token)
        assert rollup_a_token.total_quantity == 3000
        assert rollup_a_token.total_cost_micro_cents == 450_000

        # ── Execution 3: Incremental addition in same period ──────────────────
        ev_new = UsageEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            idempotency_key="ev_new",
            usage_type="token",
            quantity=500,
            cost_micro_cents=75_000,
            billing_period_start=period,
            payload_hash="h_new",
            response_payload={"result": "ok"},
        )
        db_session.add(ev_new)
        db_session.commit()

        rollup_service.compute_monthly_rollups(db_session, period)
        db_session.refresh(rollup_a_token)
        # Should now be exactly 3000 + 500 = 3500 (not double counted to 6500)
        assert rollup_a_token.total_quantity == 3500
        assert rollup_a_token.total_cost_micro_cents == 525_000

    def test_month_boundary_period_isolation(
        self, db_session: Session, tenant_a: Tenant
    ):
        rollup_service = RollupService()
        jan_period = date(2026, 1, 1)
        feb_period = date(2026, 2, 1)

        # Event on last day of January (period = 2026-01-01)
        ev_jan = UsageEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            idempotency_key="ev_jan_end",
            usage_type="token",
            quantity=1000,
            cost_micro_cents=150_000,
            billing_period_start=jan_period,
            payload_hash="h_jan",
            response_payload={"result": "ok"},
        )

        # Event on first day of February (period = 2026-02-01)
        ev_feb = UsageEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            idempotency_key="ev_feb_start",
            usage_type="token",
            quantity=2000,
            cost_micro_cents=300_000,
            billing_period_start=feb_period,
            payload_hash="h_feb",
            response_payload={"result": "ok"},
        )

        db_session.add_all([ev_jan, ev_feb])
        db_session.commit()

        # Compute January rollup
        rollup_service.compute_monthly_rollups(db_session, jan_period)
        jan_rollup = db_session.execute(
            select(MonthlyUsageRollup).where(
                MonthlyUsageRollup.tenant_id == tenant_a.id,
                MonthlyUsageRollup.billing_period_start == jan_period,
            )
        ).scalars().first()
        assert jan_rollup.total_quantity == 1000

        # Compute February rollup
        rollup_service.compute_monthly_rollups(db_session, feb_period)
        feb_rollup = db_session.execute(
            select(MonthlyUsageRollup).where(
                MonthlyUsageRollup.tenant_id == tenant_a.id,
                MonthlyUsageRollup.billing_period_start == feb_period,
            )
        ).scalars().first()
        assert feb_rollup.total_quantity == 2000

