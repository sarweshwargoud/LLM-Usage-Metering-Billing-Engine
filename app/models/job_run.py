"""
JobRun model.

Tracks execution of background jobs for observability, idempotency,
and prevention of duplicate concurrent execution.

Design:
- (job_name, billing_period) uniqueness prevents running the same job
  for the same period concurrently (when enforced at application layer).
- status machine: pending → running → completed | failed
- failure_detail stores the exception traceback for debugging.
- attempt_number supports retry tracking.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, Date, Index, Text, UniqueConstraint, func
from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    job_name: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Identifier of the job, e.g. 'monthly_rollup'",
    )
    # Optional — some jobs are period-specific (rollup), others are not (cleanup)
    billing_period: Mapped[object | None] = mapped_column(
        Date,
        nullable=True,
        comment="The billing period this job run targets (null for period-agnostic jobs)",
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="pending",
        comment="pending | running | completed | failed",
    )
    attempt_number: Mapped[int] = mapped_column(
        nullable=False,
        server_default="1",
        comment="Retry attempt number (1 = first attempt)",
    )
    started_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the job started executing",
    )
    completed_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the job completed successfully",
    )
    failed_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the job failed",
    )
    failure_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Exception message and traceback on failure",
    )
    rows_processed: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Number of records processed by this job run",
    )
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="chk_job_runs_status_valid",
        ),
        CheckConstraint(
            "attempt_number >= 1",
            name="chk_job_runs_attempt_number_positive",
        ),
        # Prevent a second RUNNING instance of the same job+period.
        # Application must UPDATE status away from 'running' before
        # a new run can start. Partial unique index covers only 'running' rows.
        Index(
            "uix_job_runs_active_job",
            "job_name",
            "billing_period",
            unique=True,
            postgresql_where="status = 'running'",
        ),
        # Fast lookup for monitoring / status queries
        Index("ix_job_runs_job_name_status", "job_name", "status"),
        Index("ix_job_runs_billing_period", "billing_period"),
        {
            "comment": (
                "Background job execution log. "
                "Partial unique index prevents duplicate concurrent runs."
            )
        },
    )

    def __repr__(self) -> str:
        return (
            f"<JobRun id={self.id} job={self.job_name!r} "
            f"period={self.billing_period} status={self.status!r}>"
        )
