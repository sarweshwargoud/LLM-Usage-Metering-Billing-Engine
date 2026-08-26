"""
Plan model.

A Plan defines feature limits and pricing tier.
Plans are system-level records (not tenant-owned), seeded during setup.

Design:
- name UNIQUE (e.g. "free", "pro")
- api_call_limit / token_limit stored as BIGINT
- No floating point — prices are stored in micro-cents as BIGINT
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Index, Text, func
from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
        comment="Plan slug, e.g. 'free', 'pro'",
    )
    display_name: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Human-readable plan name",
    )
    # ── Usage limits ──────────────────────────────────────────────────────
    api_call_limit: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Max API calls per billing period (0 = unlimited)",
    )
    token_limit: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Max AI tokens per billing period (0 = unlimited)",
    )
    # ── Pricing — BIGINT micro-cents, NEVER float ─────────────────────────
    # Price is what Stripe charges in cents per month (for display/Stripe)
    price_cents_per_month: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        comment="Subscription price in cents/month (0 = free tier)",
    )
    stripe_price_id: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        unique=True,
        comment="Stripe Price ID for this plan (null for free tier)",
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

    __table_args__ = (
        CheckConstraint("api_call_limit >= 0", name="chk_plans_api_call_limit_non_negative"),
        CheckConstraint("token_limit >= 0", name="chk_plans_token_limit_non_negative"),
        CheckConstraint("price_cents_per_month >= 0", name="chk_plans_price_non_negative"),
        Index("ix_plans_name", "name"),
        {"comment": "System-level plan definitions — not tenant-owned"},
    )

    def __repr__(self) -> str:
        return f"<Plan name={self.name!r} api_limit={self.api_call_limit}>"
