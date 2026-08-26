"""
WebhookEvent model.

Records every Stripe webhook event received.
Used for deduplication and retry tracking.

Correctness invariants:
- Scenario 9:  stripe_event_id PRIMARY KEY prevents duplicate processing.
- Scenario 12: Three-state status machine (processing → processed | failed)
               prevents stale-lock silencing Stripe retries.
               processing_started_at enables staleness detection (> 5 min).

State machine:
    [new delivery] → INSERT status='processing'
                          ↓
                   [process event]
                          ↓
                   UPDATE status='processed'  ──→ (done)
                          ↓ (on error)
                   UPDATE status='failed'
                          ↓
    [Stripe retry] → SELECT existing row
                   → if status='processed': return 200 (no-op)
                   → if status='processing' AND age < 5 min: return 503
                   → if status='processing' AND age > 5 min: re-process (stale lock)
                   → if status='failed': re-process
"""
from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, Text, func
from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    # Stripe event ID as the primary key — globally unique per Stripe account
    stripe_event_id: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        comment="Stripe event ID (e.g. evt_xxx) — globally unique, used for deduplication",
    )
    event_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Stripe event type (e.g. 'customer.subscription.updated')",
    )
    # ── Three-state status machine (Scenario 12) ──────────────────────────
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="processing",
        comment="processing | processed | failed",
    )
    processing_started_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment=(
            "When processing began. "
            "Used to detect stale locks: "
            "if status='processing' AND now()-processing_started_at > 5min → retry."
        ),
    )
    processed_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When processing completed successfully; null if not yet processed",
    )
    failed_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When processing failed; null if not failed",
    )
    failure_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Error message if status='failed'",
    )
    retry_count: Mapped[int] = mapped_column(
        # Note: Using int mapped to BIGINT via BigInteger would be overkill here;
        # INTEGER is sufficient for retry counts (max ~2B retries before overflow).
        # SQLAlchemy maps Python int → INTEGER by default.
        # We keep this as a plain integer.
        nullable=False,
        server_default="0",
        comment="Number of times processing has been attempted",
    )
    # Raw event stored for debugging / re-processing
    stripe_event_created: Mapped[object | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "Stripe event 'created' unix timestamp converted to DateTime(timezone=True). "
            "Used for out-of-order webhook detection (Scenario 10)."
        ),
    )
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When this row was first inserted into our database",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'processed', 'failed')",
            name="chk_webhook_events_status_valid",
        ),
        CheckConstraint(
            "retry_count >= 0",
            name="chk_webhook_events_retry_count_non_negative",
        ),
        # Fast lookup for status transitions
        Index("ix_webhook_events_status", "status"),
        # Find stale processing rows for the cleanup/retry job
        Index(
            "ix_webhook_events_status_started",
            "status",
            "processing_started_at",
        ),
        {
            "comment": (
                "Records every received Stripe webhook event. "
                "PK on stripe_event_id prevents duplicate processing."
            )
        },
    )

    def __repr__(self) -> str:
        return (
            f"<WebhookEvent id={self.stripe_event_id!r} "
            f"type={self.event_type!r} status={self.status!r}>"
        )
