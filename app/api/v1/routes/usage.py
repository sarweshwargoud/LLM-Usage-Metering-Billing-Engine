"""
Tenant Usage endpoint (/usage).
"""
from __future__ import annotations

from datetime import date
from typing import Annotated
from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, get_current_tenant
from app.database import get_db
from app.models.quota_counter import QuotaCounter
from app.models.subscription import Subscription
from app.models.usage_event import UsageEvent
from app.schemas.usage import UsageDetail, UsageSummaryResponse
from app.services.quota_enforcer import QuotaEnforcer

router = APIRouter(tags=["Usage"])
quota_enforcer = QuotaEnforcer()


@router.get("/usage", response_model=UsageSummaryResponse, status_code=status.HTTP_200_OK)
def get_tenant_usage(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    db: Annotated[Session, Depends(get_db)],
) -> UsageSummaryResponse:
    """
    Returns current billing period usage metrics for the authenticated tenant.
    Guarantees tenant isolation: tenant_id is strictly derived from the verified JWT.
    """
    current_period = date.today().replace(day=1)

    # 1. Fetch active subscription
    sub = db.execute(
        select(Subscription)
        .where(
            Subscription.tenant_id == tenant.id,
            Subscription.status == "active",
        )
        .order_by(Subscription.created_at.desc())
    ).scalars().first()

    plan_name = sub.plan if sub else "free"
    sub_status = sub.status if sub else "active"

    # 2. Fetch quota counters for current period (or initialize defaults)
    quota_counters = db.execute(
        select(QuotaCounter).where(
            QuotaCounter.tenant_id == tenant.id,
            QuotaCounter.billing_period_start == current_period,
        )
    ).scalars().all()

    counters_by_type = {c.usage_type: c for c in quota_counters}

    # 3. Fetch cumulative cost by usage_type
    cost_aggregates = db.execute(
        select(
            UsageEvent.usage_type,
            func.coalesce(func.sum(UsageEvent.cost_micro_cents), 0).label("total_cost"),
        )
        .where(
            UsageEvent.tenant_id == tenant.id,
            UsageEvent.billing_period_start == current_period,
        )
        .group_by(UsageEvent.usage_type)
    ).all()

    cost_by_type = {row[0]: row[1] for row in cost_aggregates}

    # Build response structure for known usage types
    usage_dict: dict[str, UsageDetail] = {}
    for utype in ("api_call", "token"):
        counter = counters_by_type.get(utype)
        limit = counter.plan_limit if counter else quota_enforcer.get_plan_limit(db, tenant.id, utype)
        used = counter.used if counter else 0
        remaining = max(0, limit - used)
        total_cost = cost_by_type.get(utype, 0)

        usage_dict[utype] = UsageDetail(
            usage_type=utype,
            used=used,
            plan_limit=limit,
            remaining=remaining,
            total_cost_micro_cents=total_cost,
        )

    return UsageSummaryResponse(
        tenant_id=str(tenant.id),
        tenant_name=tenant.name,
        plan=plan_name,
        subscription_status=sub_status,
        billing_period_start=str(current_period),
        usage=usage_dict,
    )
