"""
Subscription model.

A Subscription records a tenant's current plan and Stripe subscription state.
Stripe is the source of truth — this table mirrors Stripe state via verified webhooks.

Correctness invariants (from failure-scenario review):
- Scenario 10: last_event_timestamp prevents out-of-order webhook application.
  Only apply a webhook event if event.created > last_event_timestamp.
- Scenario 14: cancelled_at records cancellation time for proration/audit.
- stripe_subscription_id UNIQUE prevents duplicate subscriptions.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    DateTime,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    func,
)
from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owner tenant — sourced from verified JWT, never from client body",
    )
    # ── Stripe mirroring ──────────────────────────────────────────────────
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        unique=True,   # UNIQUE — prevents duplicate subscription creation
        comment="Stripe Subscription ID — null for free tier (no Stripe subscription)",
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Stripe Customer ID — denormalised for fast webhook lookup",
    )
    plan: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="free",
        comment="Current plan slug: 'free' | 'pro'",
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="active",
        comment="Subscription status mirrored from Stripe",
    )
    # ── Out-of-order webhook protection (Scenario 10) ─────────────────────
    last_event_timestamp: Mapped[object | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "Timestamp of the most recently applied Stripe event. "
            "Reject any event with created <= this value (out-of-order guard)."
        ),
    )
    # ── Cancellation (Scenario 14) ────────────────────────────────────────
    cancelled_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="UTC timestamp when subscription was cancelled; null if active",
    )
    current_period_start: Mapped[object | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Start of current Stripe billing period",
    )
    current_period_end: Mapped[object | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="End of current Stripe billing period",
    )
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ─────────────────────────────────────────────────────
    tenant: Mapped["Tenant"] = relationship(back_populates="subscriptions")  # noqa: F821

    __table_args__ = (
        CheckConstraint(
            "plan IN ('free', 'pro')",
            name="chk_subscriptions_plan_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'past_due', 'cancelled', 'trialing', 'incomplete')",
            name="chk_subscriptions_status_valid",
        ),
        # Each tenant should only have one subscription at a time.
        # Enforced at application layer; unique index on tenant_id is
        # intentionally omitted to allow subscription history if needed.
        Index("ix_subscriptions_tenant_id", "tenant_id"),
        Index("ix_subscriptions_stripe_subscription_id", "stripe_subscription_id"),
        Index("ix_subscriptions_stripe_customer_id", "stripe_customer_id"),
        {
            "comment": (
                "Mirrors Stripe subscription state. "
                "Stripe is the source of truth — updated only via verified webhooks."
            )
        },
    )

    def __repr__(self) -> str:
        return (
            f"<Subscription tenant={self.tenant_id} plan={self.plan!r} "
            f"status={self.status!r}>"
        )
