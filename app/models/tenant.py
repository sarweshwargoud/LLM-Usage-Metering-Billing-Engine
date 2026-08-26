"""
Tenant model.

A tenant is the top-level billing entity — one organisation or user account.
Every other billable table references tenant_id.

Design:
- UUID PK (opaque, safe to put in JWTs and URLs)
- email UNIQUE (used for login and Stripe customer lookup)
- stripe_customer_id nullable (set after first Stripe interaction)
- created_at / updated_at for audit
"""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, DateTime, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

# SQLAlchemy 2.0: use DateTime(timezone=True) which maps to DateTime(timezone=True) in PostgreSQL
_TIMESTAMPTZ = DateTime(timezone=True)

from app.models.base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Opaque tenant identifier — sourced from verified JWT only",
    )
    name: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Display name of the tenant organisation",
    )
    email: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
        comment="Unique email — used for login and Stripe customer lookup",
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        unique=True,
        comment="Stripe customer ID — null until first Stripe interaction",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
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

    # ── Relationships (defined here so FK cascade can be configured) ──────
    subscriptions: Mapped[list["Subscription"]] = relationship(  # noqa: F821
        back_populates="tenant", cascade="all, delete-orphan"
    )
    usage_events: Mapped[list["UsageEvent"]] = relationship(  # noqa: F821
        back_populates="tenant", cascade="all, delete-orphan"
    )
    quota_counters: Mapped[list["QuotaCounter"]] = relationship(  # noqa: F821
        back_populates="tenant", cascade="all, delete-orphan"
    )
    monthly_rollups: Mapped[list["MonthlyUsageRollup"]] = relationship(  # noqa: F821
        back_populates="tenant", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Fast lookup by email (login path)
        Index("ix_tenants_email", "email"),
        # Fast lookup by Stripe customer ID (webhook path)
        Index("ix_tenants_stripe_customer_id", "stripe_customer_id"),
        {"comment": "Top-level billing entity — every billable row references this"},
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} email={self.email!r}>"
