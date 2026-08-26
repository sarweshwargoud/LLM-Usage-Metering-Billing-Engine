"""
Tests for Background Job Runner, Locking, Crash Recovery, and Retries.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.job_run import JobRun
from app.models.tenant import Tenant
from app.models.usage_event import UsageEvent
from app.services.job_runner import JobRunner


class TestJobRunner:
    """Tests job locking, concurrency, crash recovery, and execution."""

    def test_job_runner_starts_and_completes_monthly_rollup(
        self, db_session: Session, tenant_a: Tenant
    ):
        runner = JobRunner()
        period = date(2026, 4, 1)

        # Seed usage event
        ev = UsageEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            idempotency_key="ev_job_01",
            usage_type="token",
            quantity=1500,
            cost_micro_cents=225_000,
            billing_period_start=period,
            payload_hash="h_job_01",
            response_payload={"result": "ok"},
        )
        db_session.add(ev)
        db_session.commit()

        result = runner.run_monthly_rollup_job(db_session, period)
        assert result["status"] == "completed"
        assert result["rows_processed"] == 1
        assert result["attempt_number"] == 1

        # Verify JobRun record in DB
        job = db_session.execute(
            select(JobRun).where(JobRun.id == result["job_run_id"])
        ).scalars().first()
        assert job is not None
        assert job.status == "completed"
        assert job.completed_at is not None
        assert job.rows_processed == 1

    def test_concurrent_job_execution_prevented(
        self, db_session: Session
    ):
        runner = JobRunner()
        period = date(2026, 4, 1)

        # First worker starts job
        job1 = runner.start_job(db_session, "monthly_rollup", period)
        assert job1 is not None
        assert job1.status == "running"

        # Second worker attempts to start the exact same job for the same period
        job2 = runner.start_job(db_session, "monthly_rollup", period)
        assert job2 is None  # Concurrency lock prevented duplicate run

    def test_stale_job_timeout_recovery(
        self, db_session: Session
    ):
        runner = JobRunner()
        period = date(2026, 5, 1)

        # Simulate a crashed job started 20 minutes ago
        crashed_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        crashed_job = JobRun(
            job_name="monthly_rollup",
            billing_period=period,
            status="running",
            attempt_number=1,
            started_at=crashed_time,
        )
        db_session.add(crashed_job)
        db_session.commit()

        # New runner attempts to run the job
        new_job = runner.start_job(db_session, "monthly_rollup", period)
        assert new_job is not None
        assert new_job.status == "running"
        assert new_job.attempt_number == 2

        # Verify crashed job was marked as failed
        db_session.refresh(crashed_job)
        assert crashed_job.status == "failed"
        assert "Stale job timeout" in (crashed_job.failure_detail or "")

    def test_job_failure_allows_safe_retry(
        self, db_session: Session
    ):
        runner = JobRunner()
        period = date(2026, 6, 1)

        # Run 1: Force failure during rollup execution
        with patch("app.services.rollup_service.RollupService.compute_monthly_rollups", side_effect=RuntimeError("Simulated DB Disk Failure")):
            res1 = runner.run_monthly_rollup_job(db_session, period)
            assert res1["status"] == "failed"
            assert "Simulated DB Disk Failure" in res1["error"]
            assert res1["attempt_number"] == 1

        # Run 2: Retry succeeds
        res2 = runner.run_monthly_rollup_job(db_session, period)
        assert res2["status"] == "completed"
        assert res2["attempt_number"] == 2
