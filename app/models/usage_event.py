"""
UsageEvent model.

Records a single billable operation (one API call or one token batch).

Correctness invariants (from failure-scenario review):
- Scenario 1:  response_payload JSONB stores the exact HTTP response for replay.
- Scenario 2:  payload_hash TEXT detects same-key / different-payload attacks.
- Scenario 3:  UNIQUE(tenant_id, idempotency_key) ensures exactly-once insertion.
- Scenario 17: quantity BIGINT + CHECK(BETWEEN 1 AND 1_000_000) prevents overflow.
- Scenario 16: cost_micro_cents BIGINT — never float, never NUMERIC.
- Scenario 19: billing_period_start DATE set at insert time, not at aggregation time.

The idempotency key is owned by the caller.  The payload_hash is computed
by the server from the canonical request body so that a mismatched replay
can be detected (Scenario 2).

Atomic quota check is performed in quota_counters, NOT here (no TOCTOU).
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    DateTime,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Opaque event identifier",
    )
    # ── Tenant isolation (Scenario 15) ────────────────────────────────────
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant owner — ALWAYS sourced from verified JWT, never from client body",
    )
    # ── Idempotency (Scenarios 1, 2, 3) ───────────────────────────────────
    idempotency_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Client-supplied idempotency key — unique per tenant",
    )
    payload_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment=(
            "SHA-256 of the canonical request body (usage_type + quantity + metadata). "
            "Used to detect same-key / different-payload attacks (Scenario 2)."
        ),
    )
    # ── Usage classification ───────────────────────────────────────────────
    usage_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Usage category: 'api_call' | 'token'",
    )
    quantity: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Number of units consumed — BIGINT, validated 1..1_000_000 at API layer",
    )
    # ── Monetary value — integer micro-cents (Scenario 16) ────────────────
    # 1 micro-cent = 1/100 cent = 1/10,000 dollar
    # No FLOAT, REAL, DOUBLE PRECISION, or NUMERIC here — always BIGINT.
    cost_micro_cents: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        comment="Cost in micro-cents (integer only — NEVER float)",
    )
    # ── Billing period (Scenario 19) ──────────────────────────────────────
    billing_period_start: Mapped[object] = mapped_column(
        Date,
        nullable=False,
        comment=(
            "First day of the billing month at insert time "
            "(date_trunc('month', now())::date). "
            "Set atomically on insert — never changed after the fact."
        ),
    )
    # ── Idempotency replay storage (Scenario 1) ───────────────────────────
    response_payload: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment=(
            "Exact HTTP response body serialised as JSON. "
            "Returned verbatim on idempotent replay so the client sees an "
            "identical response even after a crash-and-retry cycle."
        ),
    )
    recorded_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Wall-clock time when the event was durably recorded",
    )

    # ── Relationships ─────────────────────────────────────────────────────
    tenant: Mapped["Tenant"] = relationship(back_populates="usage_events")  # noqa: F821

    __table_args__ = (
        # ── Primary correctness constraint (Scenario 3) ────────────────────
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_usage_events_tenant_idempotency",
        ),
        # ── Quantity bounds (Scenario 17) ─────────────────────────────────
        CheckConstraint(
            "quantity BETWEEN 1 AND 1000000",
            name="chk_usage_events_quantity_range",
        ),
        # ── Monetary sanity ───────────────────────────────────────────────
        CheckConstraint(
            "cost_micro_cents >= 0",
            name="chk_usage_events_cost_non_negative",
        ),
        # ── Usage type allowlist ──────────────────────────────────────────
        CheckConstraint(
            "usage_type IN ('api_call', 'token')",
            name="chk_usage_events_usage_type_valid",
        ),
        # ── Indexes for common query paths ────────────────────────────────
        # Tenant usage aggregation (GET /usage)
        Index(
            "ix_usage_events_tenant_period",
            "tenant_id",
            "billing_period_start",
        ),
        # Idempotency lookup (hot path — every POST /generate)
        Index(
            "ix_usage_events_tenant_idempotency",
            "tenant_id",
            "idempotency_key",
        ),
        # Rollup job scans by billing period
        Index("ix_usage_events_billing_period", "billing_period_start"),
        {
            "comment": (
                "One row per billable operation. "
                "UNIQUE(tenant_id, idempotency_key) enforces exactly-once recording. "
                "payload_hash guards against same-key / different-payload attacks."
            )
        },
    )

    def __repr__(self) -> str:
        return (
            f"<UsageEvent id={self.id} tenant={self.tenant_id} "
            f"type={self.usage_type!r} qty={self.quantity}>"
        )
