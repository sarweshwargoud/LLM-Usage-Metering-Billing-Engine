"""
Phase 1 Database Foundation Tests.

Tests proving the correctness invariants required by the failure-scenario review.
Each test directly exercises the database layer (no HTTP, no services).

Coverage:
 1. Duplicate idempotency key rejected by database (UNIQUE constraint)
 2. Different tenants can use the same idempotency key (isolation)
 3. quantity = 0 rejected by CHECK constraint
 4. quantity > 1_000_000 rejected by CHECK constraint
 5. BIGINT columns exist for quantity / counters / money
 6. Duplicate Stripe event IDs rejected (PK on webhook_events)
 7. Duplicate monthly rollup key rejected (UNIQUE constraint)
 8. Tenant foreign keys work correctly (FK + CASCADE)
 9. Subscription stripe_subscription_id is UNIQUE
10. Quota counter PK correctly includes billing_period_start
11. Monetary columns are integer-based (BigInteger, not FLOAT/NUMERIC)
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    MonthlyUsageRollup,
    QuotaCounter,
    Subscription,
    Tenant,
    UsageEvent,
    WebhookEvent,
)


# ─── Helpers ──────────────────────────────────────────────────────────────

def _make_usage_event(
    tenant_id: uuid.UUID,
    idempotency_key: str = "key-001",
    quantity: int = 10,
    usage_type: str = "api_call",
    billing_period: date | None = None,
) -> UsageEvent:
    if billing_period is None:
        billing_period = date.today().replace(day=1)
    return UsageEvent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        payload_hash="abc123hash",
        usage_type=usage_type,
        quantity=quantity,
        cost_micro_cents=100,
        billing_period_start=billing_period,
        response_payload={"cost_micro_cents": 100},
    )


def _make_quota_counter(
    tenant_id: uuid.UUID,
    usage_type: str = "api_call",
    billing_period: date | None = None,
    plan_limit: int = 1000,
    used: int = 0,
) -> QuotaCounter:
    if billing_period is None:
        billing_period = date.today().replace(day=1)
    return QuotaCounter(
        tenant_id=tenant_id,
        usage_type=usage_type,
        billing_period_start=billing_period,
        plan_limit=plan_limit,
        used=used,
    )


# ═════════════════════════════════════════════════════════════════════════
# TEST 1 — Duplicate idempotency key is rejected by the database
# Scenario 3 from failure-scenario review
# ═════════════════════════════════════════════════════════════════════════

class TestIdempotencyKeyUniqueness:
    def test_duplicate_key_same_tenant_raises_integrity_error(
        self, db_session: Session, tenant_a: Tenant
    ):
        """
        UNIQUE(tenant_id, idempotency_key) must cause an IntegrityError
        when the same key is used twice for the same tenant.
        This is the database-level enforcement of Scenario 3.
        """
        key = "duplicate-key-test-001"
        event1 = _make_usage_event(tenant_a.id, idempotency_key=key, quantity=5)
        db_session.add(event1)
        db_session.flush()

        event2 = _make_usage_event(tenant_a.id, idempotency_key=key, quantity=10)
        db_session.add(event2)

        with pytest.raises(IntegrityError, match="uq_usage_events_tenant_idempotency"):
            db_session.flush()

    def test_exactly_one_row_per_unique_key(
        self, db_session: Session, tenant_a: Tenant
    ):
        """
        A valid unique key must produce exactly 1 row after a successful insert.
        This verifies the UNIQUE constraint is per-(tenant, key) and does not
        accidentally produce phantom rows or double-count.
        """
        key = "unique-single-insert-001"
        event = _make_usage_event(tenant_a.id, idempotency_key=key)
        db_session.add(event)
        db_session.flush()

        count = db_session.execute(
            text(
                "SELECT COUNT(*) FROM usage_events "
                "WHERE tenant_id=:tid AND idempotency_key=:key"
            ),
            {"tid": str(tenant_a.id), "key": key},
        ).scalar()
        assert count == 1, f"Expected exactly 1 row, got {count}"


# ═════════════════════════════════════════════════════════════════════════
# TEST 2 — Different tenants CAN use the same idempotency key
# ═════════════════════════════════════════════════════════════════════════

class TestCrossTenantIdempotencyIsolation:
    def test_same_key_different_tenants_is_allowed(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        """
        The UNIQUE constraint is (tenant_id, idempotency_key) — NOT just (idempotency_key).
        Tenant A and Tenant B must be able to use the same idempotency key independently.
        """
        shared_key = "shared-key-across-tenants"
        event_a = _make_usage_event(tenant_a.id, idempotency_key=shared_key)
        event_b = _make_usage_event(tenant_b.id, idempotency_key=shared_key)

        db_session.add_all([event_a, event_b])
        # Must not raise — different tenants are fine
        db_session.flush()

        count = db_session.execute(
            text("SELECT COUNT(*) FROM usage_events WHERE idempotency_key=:key"),
            {"key": shared_key},
        ).scalar()
        assert count == 2  # One row per tenant


# ═════════════════════════════════════════════════════════════════════════
# TEST 3 — quantity = 0 is rejected by CHECK constraint
# Scenario 17 from failure-scenario review
# ═════════════════════════════════════════════════════════════════════════

class TestQuantityCheckConstraint:
    def test_quantity_zero_raises_integrity_error(
        self, db_session: Session, tenant_a: Tenant
    ):
        """
        CHECK (quantity BETWEEN 1 AND 1000000) must reject quantity=0.
        """
        event = _make_usage_event(tenant_a.id, idempotency_key="qty-zero-001", quantity=0)
        db_session.add(event)
        with pytest.raises(IntegrityError, match="chk_usage_events_quantity_range"):
            db_session.flush()

    def test_quantity_one_is_allowed(
        self, db_session: Session, tenant_a: Tenant
    ):
        """Lower bound: quantity=1 must be accepted."""
        event = _make_usage_event(tenant_a.id, idempotency_key="qty-one-001", quantity=1)
        db_session.add(event)
        db_session.flush()  # Must not raise

    def test_quantity_1000000_is_allowed(
        self, db_session: Session, tenant_a: Tenant
    ):
        """Upper bound: quantity=1_000_000 must be accepted."""
        event = _make_usage_event(
            tenant_a.id, idempotency_key="qty-max-001", quantity=1_000_000
        )
        db_session.add(event)
        db_session.flush()  # Must not raise


# ═════════════════════════════════════════════════════════════════════════
# TEST 4 — quantity > 1,000,000 is rejected by CHECK constraint
# Scenario 17 from failure-scenario review
# ═════════════════════════════════════════════════════════════════════════

class TestQuantityOverflowRejection:
    @pytest.mark.parametrize("bad_qty", [1_000_001, 2**31 - 1, 2**32, 9_999_999_999])
    def test_quantity_over_limit_rejected(
        self, db_session: Session, tenant_a: Tenant, bad_qty: int
    ):
        """Any quantity > 1_000_000 must fail CHECK constraint."""
        event = _make_usage_event(
            tenant_a.id,
            idempotency_key=f"qty-over-{bad_qty}",
            quantity=bad_qty,
        )
        db_session.add(event)
        with pytest.raises(IntegrityError, match="chk_usage_events_quantity_range"):
            db_session.flush()

    def test_negative_quantity_rejected(
        self, db_session: Session, tenant_a: Tenant
    ):
        """Negative quantity must also fail CHECK constraint."""
        event = _make_usage_event(
            tenant_a.id, idempotency_key="qty-neg-001", quantity=-1
        )
        db_session.add(event)
        with pytest.raises(IntegrityError, match="chk_usage_events_quantity_range"):
            db_session.flush()


# ═════════════════════════════════════════════════════════════════════════
# TEST 5 — BIGINT columns exist for quantity / counters / money
# Scenario 17 from failure-scenario review
# ═════════════════════════════════════════════════════════════════════════

class TestBigIntColumnTypes:
    """
    Inspect the database schema directly to verify column types.
    PostgreSQL maps BIGINT to 'BIGINT' in information_schema.
    """

    def _get_column_type(self, db_session: Session, table: str, column: str) -> str:
        row = db_session.execute(
            text("""
                SELECT data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name   = :table
                  AND column_name  = :column
            """),
            {"table": table, "column": column},
        ).fetchone()
        assert row is not None, f"Column {table}.{column} not found in schema"
        return row[0].upper()

    def test_usage_events_quantity_is_bigint(self, db_session: Session):
        col_type = self._get_column_type(db_session, "usage_events", "quantity")
        assert col_type == "BIGINT", f"Expected BIGINT, got {col_type}"

    def test_usage_events_cost_micro_cents_is_bigint(self, db_session: Session):
        col_type = self._get_column_type(db_session, "usage_events", "cost_micro_cents")
        assert col_type == "BIGINT", f"Expected BIGINT, got {col_type}"

    def test_quota_counters_used_is_bigint(self, db_session: Session):
        col_type = self._get_column_type(db_session, "quota_counters", "used")
        assert col_type == "BIGINT", f"Expected BIGINT, got {col_type}"

    def test_quota_counters_plan_limit_is_bigint(self, db_session: Session):
        col_type = self._get_column_type(db_session, "quota_counters", "plan_limit")
        assert col_type == "BIGINT", f"Expected BIGINT, got {col_type}"

    def test_monthly_rollups_total_quantity_is_bigint(self, db_session: Session):
        col_type = self._get_column_type(
            db_session, "monthly_usage_rollups", "total_quantity"
        )
        assert col_type == "BIGINT", f"Expected BIGINT, got {col_type}"

    def test_monthly_rollups_total_cost_micro_cents_is_bigint(self, db_session: Session):
        col_type = self._get_column_type(
            db_session, "monthly_usage_rollups", "total_cost_micro_cents"
        )
        assert col_type == "BIGINT", f"Expected BIGINT, got {col_type}"

    def test_no_float_columns_for_money(self, db_session: Session):
        """
        None of the money-related columns should use FLOAT, REAL,
        or DOUBLE PRECISION — they must all be BIGINT.
        """
        float_money_columns = db_session.execute(
            text("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND data_type IN ('real', 'double precision', 'float', 'numeric')
                  AND (column_name LIKE '%cost%'
                    OR column_name LIKE '%price%'
                    OR column_name LIKE '%amount%'
                    OR column_name LIKE '%cents%')
            """)
        ).fetchall()
        assert float_money_columns == [], (
            f"Found float/numeric money columns (must be BIGINT): {float_money_columns}"
        )


# ═════════════════════════════════════════════════════════════════════════
# TEST 6 — Duplicate Stripe event IDs are rejected
# Scenario 9 from failure-scenario review
# ═════════════════════════════════════════════════════════════════════════

class TestWebhookEventDeduplication:
    def test_duplicate_stripe_event_id_raises_integrity_error(self, db_session: Session):
        """
        stripe_event_id is the PRIMARY KEY of webhook_events.
        Inserting the same event ID twice must fail immediately.
        """
        event_id = "evt_test_duplicate_001"
        evt1 = WebhookEvent(
            stripe_event_id=event_id,
            event_type="customer.subscription.updated",
            status="processing",
        )
        db_session.add(evt1)
        db_session.flush()

        evt2 = WebhookEvent(
            stripe_event_id=event_id,
            event_type="customer.subscription.updated",
            status="processing",
        )
        db_session.add(evt2)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_different_stripe_event_ids_are_allowed(self, db_session: Session):
        """Different event IDs should co-exist without error."""
        for i in range(3):
            evt = WebhookEvent(
                stripe_event_id=f"evt_test_unique_{i:04d}",
                event_type="customer.subscription.updated",
                status="processing",
            )
            db_session.add(evt)
        db_session.flush()  # Must not raise

    def test_webhook_status_constraint(self, db_session: Session):
        """Invalid status values must be rejected."""
        evt = WebhookEvent(
            stripe_event_id="evt_bad_status_001",
            event_type="customer.subscription.updated",
            status="invalid_status",  # Not in the allowed set
        )
        db_session.add(evt)
        with pytest.raises(IntegrityError, match="chk_webhook_events_status_valid"):
            db_session.flush()


# ═════════════════════════════════════════════════════════════════════════
# TEST 7 — Duplicate monthly rollup key is rejected
# Scenario 20 from failure-scenario review
# ═════════════════════════════════════════════════════════════════════════

class TestMonthlyRollupUniqueness:
    def test_duplicate_rollup_key_raises_integrity_error(
        self, db_session: Session, tenant_a: Tenant
    ):
        """
        UNIQUE(tenant_id, billing_period_start, usage_type) enables
        ON CONFLICT DO UPDATE for idempotent rollup.
        A plain INSERT without ON CONFLICT must fail on duplicate.
        """
        period = date(2026, 1, 1)
        rollup1 = MonthlyUsageRollup(
            tenant_id=tenant_a.id,
            billing_period_start=period,
            usage_type="api_call",
            total_quantity=100,
            total_cost_micro_cents=10000,
            computed_at=datetime.now(timezone.utc),
        )
        db_session.add(rollup1)
        db_session.flush()

        rollup2 = MonthlyUsageRollup(
            tenant_id=tenant_a.id,
            billing_period_start=period,  # Same period
            usage_type="api_call",        # Same type
            total_quantity=200,
            total_cost_micro_cents=20000,
            computed_at=datetime.now(timezone.utc),
        )
        db_session.add(rollup2)
        with pytest.raises(IntegrityError, match="uq_monthly_rollups_tenant_period_type"):
            db_session.flush()

    def test_rollup_upsert_is_idempotent(
        self, db_session: Session, tenant_a: Tenant
    ):
        """
        ON CONFLICT DO UPDATE (the correct production pattern) must produce
        exactly one row with the updated values, no matter how many times it runs.
        This proves Scenario 20 is safe.
        """
        period = date(2026, 2, 1)
        upsert_sql = text("""
            INSERT INTO monthly_usage_rollups
                (tenant_id, billing_period_start, usage_type,
                 total_quantity, total_cost_micro_cents, computed_at)
            VALUES
                (:tenant_id, :period, 'api_call', :qty, :cost, now())
            ON CONFLICT (tenant_id, billing_period_start, usage_type)
            DO UPDATE SET
                total_quantity         = EXCLUDED.total_quantity,
                total_cost_micro_cents = EXCLUDED.total_cost_micro_cents,
                computed_at            = EXCLUDED.computed_at
        """)
        params = {
            "tenant_id": str(tenant_a.id),
            "period": period,
            "qty": 500,
            "cost": 50000,
        }
        # Run 3 times — idempotent; final state must match last run's values
        for _ in range(3):
            db_session.execute(upsert_sql, params)
            db_session.flush()

        count = db_session.execute(
            text("""
                SELECT COUNT(*), SUM(total_quantity)
                FROM monthly_usage_rollups
                WHERE tenant_id=:tid AND billing_period_start=:period AND usage_type='api_call'
            """),
            {"tid": str(tenant_a.id), "period": period},
        ).fetchone()
        assert count[0] == 1, "Upsert must produce exactly 1 row"
        assert count[1] == 500, "total_quantity must be the last written value, not accumulated"


# ═════════════════════════════════════════════════════════════════════════
# TEST 8 — Tenant foreign keys work correctly + CASCADE
# ═════════════════════════════════════════════════════════════════════════

class TestTenantForeignKeys:
    def test_usage_event_requires_valid_tenant(self, db_session: Session):
        """
        Inserting a usage_event with a non-existent tenant_id must fail
        with a ForeignKeyViolation.
        """
        fake_tenant_id = uuid.uuid4()
        event = _make_usage_event(fake_tenant_id, idempotency_key="orphan-001")
        db_session.add(event)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_subscription_requires_valid_tenant(self, db_session: Session):
        """Subscription must reference an existing tenant."""
        sub = Subscription(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),  # Non-existent
            plan="free",
            status="active",
        )
        db_session.add(sub)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_quota_counter_requires_valid_tenant(self, db_session: Session):
        """QuotaCounter must reference an existing tenant."""
        qc = _make_quota_counter(uuid.uuid4())  # Non-existent tenant
        db_session.add(qc)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_cascade_delete_tenant_removes_usage_events(
        self, db_session: Session
    ):
        """
        Deleting a tenant must CASCADE to usage_events.
        This tests that ON DELETE CASCADE is actually wired.
        """
        # Create tenant + event
        tenant = Tenant(
            id=uuid.uuid4(),
            name="Cascade Test Tenant",
            email=f"cascade-{uuid.uuid4().hex[:8]}@example.com",
        )
        db_session.add(tenant)
        db_session.flush()

        event = _make_usage_event(tenant.id, idempotency_key="cascade-001")
        db_session.add(event)
        db_session.flush()

        # Verify event exists
        count_before = db_session.execute(
            text("SELECT COUNT(*) FROM usage_events WHERE tenant_id=:tid"),
            {"tid": str(tenant.id)},
        ).scalar()
        assert count_before == 1

        # Delete the tenant
        db_session.delete(tenant)
        db_session.flush()

        # Event must be gone (CASCADE)
        count_after = db_session.execute(
            text("SELECT COUNT(*) FROM usage_events WHERE tenant_id=:tid"),
            {"tid": str(tenant.id)},
        ).scalar()
        assert count_after == 0, "CASCADE DELETE did not remove usage_events"


# ═════════════════════════════════════════════════════════════════════════
# TEST 9 — Subscription stripe_subscription_id is UNIQUE
# ═════════════════════════════════════════════════════════════════════════

class TestSubscriptionUniqueStripeId:
    def test_duplicate_stripe_subscription_id_rejected(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        """
        Even across different tenants, the same stripe_subscription_id
        must be rejected — it must be globally unique.
        """
        sub_id = "sub_test_duplicate_001"
        sub1 = Subscription(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            stripe_subscription_id=sub_id,
            plan="pro",
            status="active",
        )
        db_session.add(sub1)
        db_session.flush()

        sub2 = Subscription(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            stripe_subscription_id=sub_id,  # Same Stripe ID
            plan="pro",
            status="active",
        )
        db_session.add(sub2)
        with pytest.raises(IntegrityError, match="uq_subscriptions_stripe_subscription_id"):
            db_session.flush()

    def test_null_stripe_subscription_id_allowed_for_free_tier(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        """
        NULL stripe_subscription_id (free tier — no Stripe sub) must be
        allowed for multiple tenants since UNIQUE allows multiple NULLs.
        """
        sub1 = Subscription(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            stripe_subscription_id=None,
            plan="free",
            status="active",
        )
        sub2 = Subscription(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            stripe_subscription_id=None,
            plan="free",
            status="active",
        )
        db_session.add_all([sub1, sub2])
        db_session.flush()  # Must not raise — multiple NULLs are allowed

    def test_subscription_plan_check_constraint(
        self, db_session: Session, tenant_a: Tenant
    ):
        """Invalid plan values must be rejected."""
        sub = Subscription(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            plan="enterprise",  # Not in allowed values
            status="active",
        )
        db_session.add(sub)
        with pytest.raises(IntegrityError, match="chk_subscriptions_plan_valid"):
            db_session.flush()


# ═════════════════════════════════════════════════════════════════════════
# TEST 10 — Quota counter PK correctly includes billing_period_start
# Scenario 19 from failure-scenario review
# ═════════════════════════════════════════════════════════════════════════

class TestQuotaCounterPrimaryKey:
    def test_same_tenant_type_different_period_is_allowed(
        self, db_session: Session, tenant_a: Tenant
    ):
        """
        (tenant_id, usage_type, billing_period_start) is the composite PK.
        The same tenant + type with a DIFFERENT period must create a NEW row.
        This proves quotas are period-scoped — no counter reset needed.
        """
        qc_jan = _make_quota_counter(tenant_a.id, billing_period=date(2026, 1, 1))
        qc_feb = _make_quota_counter(tenant_a.id, billing_period=date(2026, 2, 1))
        db_session.add_all([qc_jan, qc_feb])
        db_session.flush()  # Must not raise

        count = db_session.execute(
            text("SELECT COUNT(*) FROM quota_counters WHERE tenant_id=:tid"),
            {"tid": str(tenant_a.id)},
        ).scalar()
        assert count == 2

    def test_duplicate_composite_key_rejected(
        self, db_session: Session, tenant_a: Tenant
    ):
        """
        The same (tenant_id, usage_type, billing_period_start) must be rejected.
        """
        period = date(2026, 3, 1)
        qc1 = _make_quota_counter(tenant_a.id, billing_period=period)
        db_session.add(qc1)
        db_session.flush()

        qc2 = _make_quota_counter(tenant_a.id, billing_period=period)  # Same PK
        db_session.add(qc2)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_different_usage_types_same_period_are_separate(
        self, db_session: Session, tenant_a: Tenant
    ):
        """api_call and token are independent quota counters for the same period."""
        period = date(2026, 4, 1)
        qc_api = _make_quota_counter(tenant_a.id, usage_type="api_call", billing_period=period)
        qc_tok = _make_quota_counter(tenant_a.id, usage_type="token", billing_period=period)
        db_session.add_all([qc_api, qc_tok])
        db_session.flush()  # Must not raise — different usage_type

    def test_atomic_quota_increment_sql(
        self, db_session: Session, tenant_a: Tenant
    ):
        """
        Prove the atomic quota check SQL works correctly at the DB layer.
        UPDATE ... WHERE used + qty <= plan_limit RETURNING used.

        This is the pattern that eliminates the TOCTOU race (Scenarios 7, 8).
        """
        period = date(2026, 5, 1)
        qc = _make_quota_counter(
            tenant_a.id, billing_period=period, plan_limit=100, used=95
        )
        db_session.add(qc)
        db_session.flush()

        # Request 5 units — should succeed (95 + 5 = 100 <= 100)
        result = db_session.execute(
            text("""
                UPDATE quota_counters
                SET used = used + :qty
                WHERE tenant_id = :tid
                  AND usage_type = 'api_call'
                  AND billing_period_start = :period
                  AND used + :qty <= plan_limit
                RETURNING used
            """),
            {"tid": str(tenant_a.id), "period": period, "qty": 5},
        ).fetchone()
        assert result is not None, "UPDATE must succeed (95 + 5 <= 100)"
        assert result[0] == 100

        # Request 1 more unit — should fail (100 + 1 = 101 > 100)
        result2 = db_session.execute(
            text("""
                UPDATE quota_counters
                SET used = used + :qty
                WHERE tenant_id = :tid
                  AND usage_type = 'api_call'
                  AND billing_period_start = :period
                  AND used + :qty <= plan_limit
                RETURNING used
            """),
            {"tid": str(tenant_a.id), "period": period, "qty": 1},
        ).fetchone()
        assert result2 is None, "UPDATE must return 0 rows when quota exceeded"


# ═════════════════════════════════════════════════════════════════════════
# TEST 11 — Monetary columns are integer-based (BIGINT, not FLOAT)
# Scenario 16 from failure-scenario review
# ═════════════════════════════════════════════════════════════════════════

class TestMonetaryColumnTypes:
    """
    Inspect the PostgreSQL information_schema to prove monetary columns
    are never stored as FLOAT, REAL, DOUBLE PRECISION, or NUMERIC.
    They must all be BIGINT.
    """

    def _get_col_type(self, db_session: Session, table: str, col: str) -> str:
        row = db_session.execute(
            text("""
                SELECT data_type
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name=:t
                  AND column_name=:c
            """),
            {"t": table, "c": col},
        ).fetchone()
        assert row, f"Column {table}.{col} not found"
        return row[0].upper()

    def test_usage_events_cost_micro_cents_is_bigint(self, db_session: Session):
        assert self._get_col_type(db_session, "usage_events", "cost_micro_cents") == "BIGINT"

    def test_quota_counters_plan_limit_is_bigint(self, db_session: Session):
        assert self._get_col_type(db_session, "quota_counters", "plan_limit") == "BIGINT"

    def test_quota_counters_used_is_bigint(self, db_session: Session):
        assert self._get_col_type(db_session, "quota_counters", "used") == "BIGINT"

    def test_monthly_rollups_cost_is_bigint(self, db_session: Session):
        assert self._get_col_type(
            db_session, "monthly_usage_rollups", "total_cost_micro_cents"
        ) == "BIGINT"

    def test_plans_price_cents_is_bigint(self, db_session: Session):
        assert self._get_col_type(db_session, "plans", "price_cents_per_month") == "BIGINT"

    def test_no_prohibited_money_types_in_schema(self, db_session: Session):
        """
        Exhaustive check: no column in the entire schema uses a floating-point
        type for anything named like a monetary value.
        """
        bad = db_session.execute(
            text("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND data_type IN ('real', 'double precision', 'float')
            """)
        ).fetchall()
        assert bad == [], (
            f"Found prohibited float columns in schema: {bad}"
        )

    def test_large_micro_cent_value_stored_correctly(
        self, db_session: Session, tenant_a: Tenant
    ):
        """
        BIGINT can store up to 9,223,372,036,854,775,807.
        A realistic large invoice might be 1B micro-cents ($1000).
        Verify BIGINT stores this without overflow or truncation.
        """
        large_cost = 1_000_000_000_000  # 1 trillion micro-cents = $1,000,000
        event = UsageEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            idempotency_key="large-cost-001",
            payload_hash="hashvalue",
            usage_type="token",
            quantity=1_000_000,          # Max allowed quantity
            cost_micro_cents=large_cost,
            billing_period_start=date.today().replace(day=1),
        )
        db_session.add(event)
        db_session.flush()

        stored = db_session.execute(
            text("SELECT cost_micro_cents FROM usage_events WHERE idempotency_key='large-cost-001'")
        ).scalar()
        assert stored == large_cost, (
            f"Expected {large_cost}, got {stored} — BIGINT overflow or truncation detected"
        )
