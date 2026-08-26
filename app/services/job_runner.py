"""
Background Job Runner & Concurrency Coordinator.
Manages job execution locks, crash recovery timeouts, retry tracking, and transactional execution.
"""
from __future__ import annotations

import logging
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.job_run import JobRun
from app.services.rollup_service import RollupService

logger = logging.getLogger(__name__)

STALE_JOB_TIMEOUT_MINUTES = 15


class JobRunner:
    """
    Coordinates execution of background jobs using PostgreSQL partial unique index
    and transactional status tracking for concurrency safety and crash resilience.
    """

    def start_job(
        self,
        session: Session,
        job_name: str,
        billing_period: date | None = None,
    ) -> JobRun | None:
        """
        Attempts to acquire execution lock for the given job and billing period.
        Reclaims stale running jobs older than 15 minutes.
        Returns JobRun instance if lock acquired, or None if already running.
        """
        now = datetime.now(timezone.utc)

        # ── Step 1: Check for active or stale running job ──────────────────────
        active_job = session.execute(
            select(JobRun)
            .where(
                JobRun.job_name == job_name,
                JobRun.billing_period == billing_period,
                JobRun.status == "running",
            )
            .with_for_update()
        ).scalars().first()

        if active_job is not None:
            job_start = active_job.started_at or active_job.created_at
            # Make timezone aware if needed
            if job_start.tzinfo is None:
                job_start = job_start.replace(tzinfo=timezone.utc)

            age = now - job_start
            if age > timedelta(minutes=STALE_JOB_TIMEOUT_MINUTES):
                logger.warning(
                    "Reclaiming stale job %s (id=%s) started at %s (age=%s)",
                    job_name, active_job.id, job_start, age
                )
                active_job.status = "failed"
                active_job.failed_at = now
                active_job.failure_detail = (
                    f"Stale job timeout (>{STALE_JOB_TIMEOUT_MINUTES}m) — reclaimed by runner"
                )
                session.flush()
            else:
                logger.info(
                    "Job %s for period %s is already running (id=%s, age=%s). Skipping.",
                    job_name, billing_period, active_job.id, age
                )
                return None

        # ── Step 2: Calculate attempt number ──────────────────────────────────
        prior_attempts = session.execute(
            select(func.count(JobRun.id)).where(
                JobRun.job_name == job_name,
                JobRun.billing_period == billing_period,
            )
        ).scalar() or 0
        attempt_number = prior_attempts + 1

        # ── Step 3: Insert new running job in savepoint ────────────────────────
        nested = session.begin_nested()
        try:
            job_run = JobRun(
                job_name=job_name,
                billing_period=billing_period,
                status="running",
                attempt_number=attempt_number,
                started_at=now,
            )
            session.add(job_run)
            session.flush()
            nested.commit()
            return job_run
        except IntegrityError:
            nested.rollback()
            logger.info("Concurrent worker acquired lock for job %s. Skipping.", job_name)
            return None

    def complete_job(
        self,
        session: Session,
        job_run: JobRun,
        rows_processed: int = 0,
    ) -> None:
        """Marks the job run completed."""
        job_run.status = "completed"
        job_run.completed_at = datetime.now(timezone.utc)
        job_run.rows_processed = rows_processed
        session.flush()

    def fail_job(
        self,
        session: Session,
        job_run: JobRun,
        failure_detail: str,
    ) -> None:
        """Marks the job run failed."""
        job_run.status = "failed"
        job_run.failed_at = datetime.now(timezone.utc)
        job_run.failure_detail = failure_detail[:1000]
        session.flush()

    def run_monthly_rollup_job(
        self,
        session: Session,
        billing_period: date | None = None,
    ) -> dict[str, Any]:
        """
        Executes the monthly rollup background job with full concurrency locking,
        error boundary isolation, and idempotent retry safety.
        """
        period = billing_period or date.today().replace(day=1)
        job_run = self.start_job(session, job_name="monthly_rollup", billing_period=period)

        if job_run is None:
            return {
                "status": "skipped",
                "job_name": "monthly_rollup",
                "billing_period": str(period),
                "reason": "already_running",
            }

        # Execute rollup computation inside a dedicated savepoint
        nested = session.begin_nested()
        try:
            rollup_service = RollupService()
            rows_processed = rollup_service.compute_monthly_rollups(session, period)
            nested.commit()

            self.complete_job(session, job_run, rows_processed=rows_processed)
            session.flush()

            return {
                "status": "completed",
                "job_run_id": job_run.id,
                "job_name": "monthly_rollup",
                "billing_period": str(period),
                "rows_processed": rows_processed,
                "attempt_number": job_run.attempt_number,
            }
        except Exception as exc:
            nested.rollback()
            error_trace = traceback.format_exc()
            self.fail_job(session, job_run, failure_detail=f"{exc}\n{error_trace}")
            session.flush()
            logger.exception("Monthly rollup job %s failed: %s", job_run.id, exc)
            return {
                "status": "failed",
                "job_run_id": job_run.id,
                "job_name": "monthly_rollup",
                "billing_period": str(period),
                "error": str(exc),
                "attempt_number": job_run.attempt_number,
            }
