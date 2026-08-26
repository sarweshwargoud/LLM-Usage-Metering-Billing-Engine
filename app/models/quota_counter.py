"""
QuotaCounter model.

Tracks cumulative usage for a tenant in a single billing period and usage type.
The primary key includes billing_period_start so quotas automatically reset
each month without needing a counter-reset job (Scenario 19).

CRITICAL — Atomic quota check (Scenarios 7, 8):
This table is designed specifically to support the following atomic SQL:

    UPDATE quota_counters
    SET used = used + :quantity
    WHERE tenant_id = :tenant_id
      AND usage_type = :usage_type
      AND billing_period_start = :period
      AND used + :quantity <= plan_limit
    RETURNING used;

    -- 0 rows updated → quota exceeded → reject with 429
    -- 1 row updated  → quota allowed  → proceed with INSERT into usage_events

This is the ONLY safe way to check quota.  Do NOT use:
    SELECT current_usage ...
    IF current_usage + requested <= limit:
        INSERT ...
That pattern is a TOCTOU race (Scenarios 7, 8).
"""
from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class QuotaCounter(Base):
    __tablename__ = "quota_counters"

    # ── Composite primary key (tenant + type + period) ────────────────────
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Tenant owner — part of composite PK",
    )
    usage_type: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        comment="Usage category: 'api_call' | 'token' — part of composite PK",
    )
    billing_period_start: Mapped[object] = mapped_column(
        Date,
        primary_key=True,
        comment=(
            "First day of the billing month. "
            "Part of composite PK — quotas automatically scope per period "
            "without needing a counter-reset background job (Scenario 19)."
        ),
    )
    # ── Counter values ────────────────────────────────────────────────────
    plan_limit: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment=(
            "The plan's quota limit for this usage_type this period. "
            "Denormalized from plans table for atomic comparison in UPDATE."
        ),
    )
    used: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        comment=(
            "Cumulative units consumed this period. "
            "Incremented atomically via: "
            "UPDATE ... SET used=used+qty WHERE used+qty <= plan_limit"
        ),
    )

    # ── Relationships ─────────────────────────────────────────────────────
    tenant: Mapped["Tenant"] = relationship(back_populates="quota_counters")  # noqa: F821

    __table_args__ = (
        CheckConstraint(
            "usage_type IN ('api_call', 'token')",
            name="chk_quota_counters_usage_type_valid",
        ),
        CheckConstraint(
            "plan_limit > 0",
            name="chk_quota_counters_plan_limit_positive",
        ),
        CheckConstraint(
            "used >= 0",
            name="chk_quota_counters_used_non_negative",
        ),
        CheckConstraint(
            "used <= plan_limit",
            name="chk_quota_counters_used_lte_limit",
        ),
        # Index for the atomic UPDATE hot path
        Index(
            "ix_quota_counters_tenant_type_period",
            "tenant_id",
            "usage_type",
            "billing_period_start",
        ),
        {
            "comment": (
                "Period-aware quota counter. "
                "Quota check MUST use atomic UPDATE...WHERE, never SELECT+compare."
            )
        },
    )

    def __repr__(self) -> str:
        return (
            f"<QuotaCounter tenant={self.tenant_id} type={self.usage_type!r} "
            f"period={self.billing_period_start} used={self.used}/{self.plan_limit}>"
        )
