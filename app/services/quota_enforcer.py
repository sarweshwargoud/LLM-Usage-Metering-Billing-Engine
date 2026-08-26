"""
Atomic Quota Enforcer.

Enforces period-aware quota limits using single-statement atomic PostgreSQL UPDATEs
to eliminate Time-of-Check to Time-of-Use (TOCTOU) race conditions.
"""
from __future__ import annotations

import uuid
from datetime import date
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.exceptions import QuotaExceededError, TenantNotFoundError
from app.models.plan import Plan
from app.models.quota_counter import QuotaCounter
from app.models.subscription import Subscription
from app.models.tenant import Tenant


class QuotaEnforcer:
    """
    Service for atomic quota verification and consumption.
    """

    @staticmethod
    def get_current_billing_period(for_date: date | None = None) -> date:
        """Returns the first day of the billing period (YYYY-MM-01)."""
        target = for_date or date.today()
        return target.replace(day=1)

    def get_plan_limit(self, session: Session, tenant_id: uuid.UUID, usage_type: str) -> int:
        """
        Resolves the configured plan limit for a tenant and usage type.
        Checks active subscription, falls back to free plan defaults.
        """
        settings = get_settings()

        # Check tenant exists
        tenant_exists = session.execute(
            select(Tenant.id).where(Tenant.id == tenant_id)
        ).scalar_one_or_none()
        if not tenant_exists:
            raise TenantNotFoundError(f"Tenant {tenant_id} does not exist")

        # Check active subscription
        sub = session.execute(
            select(Subscription)
            .where(
                Subscription.tenant_id == tenant_id,
                Subscription.status == "active",
            )
            .order_by(Subscription.created_at.desc())
        ).scalars().first()

        plan_name = sub.plan if sub else "free"

        # Lookup plan in database
        plan_row = session.execute(
            select(Plan).where(Plan.name == plan_name, Plan.is_active.is_(True))
        ).scalars().first()

        if plan_row:
            if usage_type == "api_call":
                return plan_row.api_call_limit
            elif usage_type == "token":
                return plan_row.token_limit

        # Fallback to configured defaults
        if plan_name == "pro":
            return (
                settings.pro_plan_api_call_limit
                if usage_type == "api_call"
                else settings.pro_plan_token_limit
            )
        return (
            settings.free_plan_api_call_limit
            if usage_type == "api_call"
            else settings.free_plan_token_limit
        )

    def ensure_quota_counter(
        self,
        session: Session,
        tenant_id: uuid.UUID,
        usage_type: str,
        billing_period_start: date,
        plan_limit: int | None = None,
    ) -> None:
        """
        Idempotently initializes the quota counter for the tenant, type, and period.
        Uses INSERT ... ON CONFLICT DO NOTHING to prevent race conditions during initialization.
        """
        if plan_limit is None:
            plan_limit = self.get_plan_limit(session, tenant_id, usage_type)

        init_sql = text("""
            INSERT INTO quota_counters (tenant_id, usage_type, billing_period_start, plan_limit, used)
            VALUES (:tenant_id, :usage_type, :billing_period_start, :plan_limit, 0)
            ON CONFLICT (tenant_id, usage_type, billing_period_start) DO NOTHING
        """)

        session.execute(
            init_sql,
            {
                "tenant_id": str(tenant_id),
                "usage_type": usage_type,
                "billing_period_start": billing_period_start,
                "plan_limit": plan_limit,
            },
        )

    def check_and_deduct(
        self,
        session: Session,
        tenant_id: uuid.UUID,
        usage_type: str,
        quantity: int,
        billing_period_start: date | None = None,
    ) -> dict[str, int]:
        """
        Atomically checks and increments quota consumption.

        Returns:
            dict containing {"used": updated_used_count, "plan_limit": limit}

        Raises:
            QuotaExceededError: if requested quantity would exceed plan limit.
            TenantNotFoundError: if tenant does not exist.
        """
        if quantity <= 0:
            raise ValueError(f"Quantity must be positive integer, got: {quantity}")

        period = billing_period_start or self.get_current_billing_period()

        # Step 1: Ensure counter exists
        self.ensure_quota_counter(session, tenant_id, usage_type, period)

        # Step 2: Atomic update in single SQL statement (TOCTOU safe)
        update_sql = text("""
            UPDATE quota_counters
            SET used = used + :quantity
            WHERE tenant_id = :tenant_id
              AND usage_type = :usage_type
              AND billing_period_start = :billing_period_start
              AND used + :quantity <= plan_limit
            RETURNING used, plan_limit
        """)

        result = session.execute(
            update_sql,
            {
                "tenant_id": str(tenant_id),
                "usage_type": usage_type,
                "billing_period_start": period,
                "quantity": quantity,
            },
        ).fetchone()

        if result is not None:
            return {"used": result[0], "plan_limit": result[1]}

        # If 0 rows returned, fetch current counter state for diagnostics
        counter = session.execute(
            select(QuotaCounter).where(
                QuotaCounter.tenant_id == tenant_id,
                QuotaCounter.usage_type == usage_type,
                QuotaCounter.billing_period_start == period,
            )
        ).scalars().first()

        current_used = counter.used if counter else 0
        limit = counter.plan_limit if counter else self.get_plan_limit(session, tenant_id, usage_type)

        raise QuotaExceededError(
            f"Quota exceeded for {usage_type}: used={current_used}, limit={limit}, requested={quantity}",
            detail={
                "usage_type": usage_type,
                "current_used": current_used,
                "plan_limit": limit,
                "requested_quantity": quantity,
                "billing_period_start": str(period),
            },
        )
