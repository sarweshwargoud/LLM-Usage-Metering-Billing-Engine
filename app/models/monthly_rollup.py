"""
MonthlyUsageRollup model.

Stores pre-computed per-tenant, per-period, per-usage-type aggregates.
Populated by the MonthlyRollupJob background task.

Correctness invariant (Scenario 20):
The UNIQUE constraint on (tenant_id, billing_period_start, usage_type) enables
idempotent upsert:

    INSERT INTO monthly_usage_rollups (...)
    SELECT tenant_id, billing_period_start, usage_type, SUM(quantity), SUM(cost_micro_cents), now()
    FROM usage_events
    WHERE billing_period_start = :period
    GROUP BY tenant_id, billing_period_start, usage_type
    ON CONFLICT (tenant_id, billing_period_start, usage_type)
    DO UPDATE SET
        total_quantity         = EXCLUDED.total_quantity,
        total_cost_micro_cents = EXCLUDED.total_cost_micro_cents,
        computed_at            = EXCLUDED.computed_at;

Running this statement multiple times produces the same result (idempotent).
"""
from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class MonthlyUsageRollup(Base):
    __tablename__ = "monthly_usage_rollups"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="Surrogate PK — the business key is (tenant_id, billing_period_start, usage_type)",
    )
    # ── Business key (drives the unique constraint for upsert) ────────────
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    billing_period_start: Mapped[object] = mapped_column(
        Date,
        nullable=False,
        comment="First day of the billing month (e.g. 2026-01-01)",
    )
    usage_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Usage category: 'api_call' | 'token'",
    )
    # ── Aggregated values — BIGINT only, never float ───────────────────────
    total_quantity: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        comment="SUM(quantity) from usage_events for this tenant+period+type",
    )
    total_cost_micro_cents: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        comment="SUM(cost_micro_cents) from usage_events — integer micro-cents, never float",
    )
    computed_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the rollup was last computed by the background job",
    )

    # ── Relationships ─────────────────────────────────────────────────────
    tenant: Mapped["Tenant"] = relationship(back_populates="monthly_rollups")  # noqa: F821

    __table_args__ = (
        # This unique constraint is the key to idempotent upsert (Scenario 20)
        UniqueConstraint(
            "tenant_id",
            "billing_period_start",
            "usage_type",
            name="uq_monthly_rollups_tenant_period_type",
        ),
        CheckConstraint(
            "usage_type IN ('api_call', 'token')",
            name="chk_monthly_rollups_usage_type_valid",
        ),
        CheckConstraint(
            "total_quantity >= 0",
            name="chk_monthly_rollups_quantity_non_negative",
        ),
        CheckConstraint(
            "total_cost_micro_cents >= 0",
            name="chk_monthly_rollups_cost_non_negative",
        ),
        Index(
            "ix_monthly_rollups_tenant_period",
            "tenant_id",
            "billing_period_start",
        ),
        {
            "comment": (
                "Pre-computed monthly aggregates. "
                "Populated by idempotent upsert — re-running the job "
                "produces the same result (Scenario 20)."
            )
        },
    )

    def __repr__(self) -> str:
        return (
            f"<MonthlyUsageRollup tenant={self.tenant_id} "
            f"period={self.billing_period_start} type={self.usage_type!r} "
            f"qty={self.total_quantity}>"
        )
