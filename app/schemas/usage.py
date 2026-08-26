"""
Pydantic schemas for the /usage endpoint.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class UsageDetail(BaseModel):
    usage_type: str = Field(description="Usage category: 'api_call' or 'token'")
    used: int = Field(description="Cumulative units consumed this billing period")
    plan_limit: int = Field(description="Configured quota limit for this period")
    remaining: int = Field(description="Remaining units available (plan_limit - used)")
    total_cost_micro_cents: int = Field(default=0, description="Total billed cost in micro-cents")


class UsageSummaryResponse(BaseModel):
    tenant_id: str = Field(description="Authenticated tenant UUID")
    tenant_name: str = Field(description="Tenant organisation name")
    plan: str = Field(description="Active subscription plan slug (e.g. 'free', 'pro')")
    subscription_status: str = Field(description="Status of active subscription (e.g. 'active')")
    billing_period_start: str = Field(description="Current billing cycle start (YYYY-MM-01)")
    usage: dict[str, UsageDetail] = Field(description="Usage metrics keyed by usage_type")
