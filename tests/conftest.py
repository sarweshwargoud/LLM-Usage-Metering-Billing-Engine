"""
Pytest configuration and shared fixtures for the test suite.

Database strategy:
- Uses a separate test PostgreSQL instance (port 5433, db billing_engine_test).
- Each test function gets a fresh transaction that is ROLLED BACK after the test.
  This means tests are isolated without needing to truncate tables.
- The schema is created once per test session using Alembic migrations.

Connection URLs:
  DATABASE_URL_SYNC for Alembic (psycopg2)
  DATABASE_URL      for application (asyncpg)

Environment:
  Set via environment variables or conftest defaults below.
  Test DB runs on docker-compose db_test service (port 5433).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

# ── Point at the TEST database ────────────────────────────────────────────
TEST_DATABASE_URL_SYNC = os.environ.get(
    "TEST_DATABASE_URL_SYNC",
    "postgresql+psycopg2://billing_test:billing_test_secret@localhost:5433/billing_engine_test",
)

# Override the app settings before importing anything from app
os.environ.setdefault("DATABASE_URL_SYNC", TEST_DATABASE_URL_SYNC)
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://billing_test:billing_test_secret@localhost:5433/billing_engine_test",
)
os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-in-production")

# ── Import app modules AFTER env overrides ────────────────────────────────
from alembic import command as alembic_command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from app.models import Base  # noqa: E402, F401 — registers all models with metadata
from app.models import (  # noqa: E402
    JobRun,
    MonthlyUsageRollup,
    Plan,
    QuotaCounter,
    Subscription,
    Tenant,
    UsageEvent,
    WebhookEvent,
)


# ═════════════════════════════════════════════════════════════════════════
# Session-scoped: apply migrations once per test run
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def db_engine():
    """
    Create a SQLAlchemy engine connected to the test database.
    Run Alembic migrations once to create the schema.
    """
    engine = create_engine(TEST_DATABASE_URL_SYNC, echo=False)
    # Run all Alembic migrations to 'head'
    alembic_cfg = AlembicConfig("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL_SYNC)
    alembic_command.upgrade(alembic_cfg, "head")
    yield engine
    engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# Function-scoped: each test runs in a savepoint, rolled back after
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def db_session(db_engine) -> Generator[Session, None, None]:
    """
    Provide a transactional database session for each test.
    Outer transaction + savepoint ensures total test isolation with rollback.
    """
    connection = db_engine.connect()
    transaction = connection.begin()

    SessionLocal = sessionmaker(
        bind=connection, autocommit=False, autoflush=False, expire_on_commit=False
    )
    session = SessionLocal()

    # Create the test isolation savepoint
    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(session, trans):
        nonlocal nested
        # If an inner savepoint ended, don't touch the test's outer savepoint
        if trans.nested:
            return
        if not connection.in_nested_transaction():
            nested = connection.begin_nested()

    yield session

    session.close()
    if nested.is_active:
        nested.rollback()
    transaction.rollback()
    connection.close()


# ═════════════════════════════════════════════════════════════════════════
# Helper fixtures — pre-built test objects
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tenant_a(db_session: Session) -> Tenant:
    """A fully committed Tenant A for use in tests."""
    t = Tenant(
        id=uuid.uuid4(),
        name="Tenant Alpha Corp",
        email=f"alpha-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add(t)
    db_session.flush()
    return t


@pytest.fixture
def tenant_b(db_session: Session) -> Tenant:
    """A second distinct tenant for isolation tests."""
    t = Tenant(
        id=uuid.uuid4(),
        name="Tenant Beta LLC",
        email=f"beta-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add(t)
    db_session.flush()
    return t


@pytest.fixture
def current_period() -> date:
    """Return the first day of the current month."""
    today = date.today()
    return today.replace(day=1)


@pytest.fixture
def quota_counter_api(db_session: Session, tenant_a: Tenant, current_period: date) -> QuotaCounter:
    """A quota counter for tenant_a, api_call type, current period with limit=1000."""
    qc = QuotaCounter(
        tenant_id=tenant_a.id,
        usage_type="api_call",
        billing_period_start=current_period,
        plan_limit=1000,
        used=0,
    )
    db_session.add(qc)
    db_session.flush()
    return qc
