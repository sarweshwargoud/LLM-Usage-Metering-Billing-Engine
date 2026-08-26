"""Initial schema — all tables for the LLM Usage Metering & Billing Engine.

Revision ID: 0001
Revises: (none — first migration)
Create Date: 2026-08-26

This migration creates the complete Phase 1 database schema as defined by:
  - The corrected architecture in failure_scenario_review.md
  - The PostgreSQL @skill (no VARCHAR, no TIMESTAMP, BIGINT for money/counters)

Tables created (in dependency order):
  1. plans            — system-level plan definitions
  2. tenants          — root billing entity
  3. subscriptions    — Stripe subscription mirror
  4. usage_events     — billable events (exactly-once, idempotent)
  5. quota_counters   — atomic period-aware quota counters
  6. webhook_events   — Stripe webhook deduplication + retry tracking
  7. monthly_usage_rollups — idempotent monthly aggregates
  8. job_runs         — background job execution log

Downgrade: drops all tables in reverse dependency order.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ══════════════════════════════════════════════════════════════════════
    # 1. plans — system-level plan definitions
    # ══════════════════════════════════════════════════════════════════════
    op.create_table(
        "plans",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False,
                  comment="Plan slug, e.g. 'free', 'pro'"),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("api_call_limit", sa.BigInteger(), nullable=False,
                  comment="Max API calls per billing period (0 = unlimited)"),
        sa.Column("token_limit", sa.BigInteger(), nullable=False,
                  comment="Max AI tokens per billing period (0 = unlimited)"),
        sa.Column("price_cents_per_month", sa.BigInteger(), nullable=False,
                  server_default="0",
                  comment="Subscription price in cents/month (0 = free tier)"),
        sa.Column("stripe_price_id", sa.Text(), nullable=True,
                  comment="Stripe Price ID — null for free tier"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_plans"),
        sa.UniqueConstraint("name", name="uq_plans_name"),
        sa.UniqueConstraint("stripe_price_id", name="uq_plans_stripe_price_id"),
        sa.CheckConstraint("api_call_limit >= 0", name="chk_plans_api_call_limit_non_negative"),
        sa.CheckConstraint("token_limit >= 0", name="chk_plans_token_limit_non_negative"),
        sa.CheckConstraint("price_cents_per_month >= 0", name="chk_plans_price_non_negative"),
        comment="System-level plan definitions — not tenant-owned",
    )
    op.create_index("ix_plans_name", "plans", ["name"])

    # ══════════════════════════════════════════════════════════════════════
    # 2. tenants — root billing entity
    # ══════════════════════════════════════════════════════════════════════
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  comment="Opaque tenant identifier — sourced from verified JWT only"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False,
                  comment="Unique email — used for login and Stripe customer lookup"),
        sa.Column("stripe_customer_id", sa.Text(), nullable=True,
                  comment="Stripe customer ID — null until first Stripe interaction"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("email", name="uq_tenants_email"),
        sa.UniqueConstraint("stripe_customer_id", name="uq_tenants_stripe_customer_id"),
        comment="Top-level billing entity — every billable row references this",
    )
    op.create_index("ix_tenants_email", "tenants", ["email"])
    op.create_index("ix_tenants_stripe_customer_id", "tenants", ["stripe_customer_id"])

    # ══════════════════════════════════════════════════════════════════════
    # 3. subscriptions — Stripe subscription mirror
    # ══════════════════════════════════════════════════════════════════════
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False,
                  comment="Owner tenant — ALWAYS sourced from verified JWT, never from client body"),
        sa.Column("stripe_subscription_id", sa.Text(), nullable=True,
                  comment="Stripe Subscription ID — null for free tier"),
        sa.Column("stripe_customer_id", sa.Text(), nullable=True,
                  comment="Stripe Customer ID — denormalised for fast webhook lookup"),
        sa.Column("plan", sa.Text(), nullable=False, server_default="free"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        # Scenario 10: out-of-order webhook protection
        sa.Column("last_event_timestamp", sa.DateTime(timezone=True), nullable=True,
                  comment="Timestamp of most recently applied Stripe event; reject events <= this"),
        # Scenario 14: cancellation tracking
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True,
                  comment="UTC timestamp when subscription was cancelled"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_subscriptions"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_subscriptions_tenant_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("stripe_subscription_id", name="uq_subscriptions_stripe_subscription_id"),
        sa.CheckConstraint("plan IN ('free', 'pro')", name="chk_subscriptions_plan_valid"),
        sa.CheckConstraint(
            "status IN ('active', 'past_due', 'cancelled', 'trialing', 'incomplete')",
            name="chk_subscriptions_status_valid",
        ),
        comment=(
            "Mirrors Stripe subscription state. "
            "Stripe is the source of truth — updated only via verified webhooks."
        ),
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"])
    op.create_index("ix_subscriptions_stripe_subscription_id", "subscriptions", ["stripe_subscription_id"])
    op.create_index("ix_subscriptions_stripe_customer_id", "subscriptions", ["stripe_customer_id"])

    # ══════════════════════════════════════════════════════════════════════
    # 4. usage_events — billable events (exactly-once, idempotent)
    # ══════════════════════════════════════════════════════════════════════
    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  comment="Opaque event identifier"),
        # Scenario 15: tenant isolation — sourced from JWT only
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False,
                  comment="Tenant owner — ALWAYS sourced from verified JWT, never from client body"),
        # Scenario 2: payload hash for mismatch detection
        sa.Column("idempotency_key", sa.Text(), nullable=False,
                  comment="Client-supplied idempotency key — unique per tenant"),
        sa.Column("payload_hash", sa.Text(), nullable=False,
                  comment="SHA-256 of canonical request body — detects same-key/different-payload attacks"),
        sa.Column("usage_type", sa.Text(), nullable=False,
                  comment="Usage category: 'api_call' | 'token'"),
        # Scenario 17: BIGINT, never INTEGER or FLOAT
        sa.Column("quantity", sa.BigInteger(), nullable=False,
                  comment="Units consumed — BIGINT, 1..1_000_000"),
        # Scenario 16: integer micro-cents, never float
        sa.Column("cost_micro_cents", sa.BigInteger(), nullable=False,
                  server_default="0",
                  comment="Cost in micro-cents — BIGINT, never FLOAT/NUMERIC"),
        # Scenario 19: billing period set at insert time
        sa.Column("billing_period_start", sa.Date(), nullable=False,
                  comment="First day of billing month — set atomically at insert"),
        # Scenario 1: store response payload for idempotent replay
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True,
                  comment="Exact HTTP response body for idempotent replay (Scenario 1)"),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_usage_events"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_usage_events_tenant_id",
            ondelete="CASCADE",
        ),
        # Scenario 3: DB-level exactly-once constraint
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key",
            name="uq_usage_events_tenant_idempotency",
        ),
        # Scenario 17: quantity bounds
        sa.CheckConstraint(
            "quantity BETWEEN 1 AND 1000000",
            name="chk_usage_events_quantity_range",
        ),
        sa.CheckConstraint(
            "cost_micro_cents >= 0",
            name="chk_usage_events_cost_non_negative",
        ),
        sa.CheckConstraint(
            "usage_type IN ('api_call', 'token')",
            name="chk_usage_events_usage_type_valid",
        ),
        comment=(
            "One row per billable operation. "
            "UNIQUE(tenant_id, idempotency_key) enforces exactly-once recording."
        ),
    )
    # Indexes — FK column (psql doesn't auto-index FKs)
    op.create_index("ix_usage_events_tenant_period", "usage_events",
                    ["tenant_id", "billing_period_start"])
    op.create_index("ix_usage_events_tenant_idempotency", "usage_events",
                    ["tenant_id", "idempotency_key"])
    op.create_index("ix_usage_events_billing_period", "usage_events",
                    ["billing_period_start"])

    # ══════════════════════════════════════════════════════════════════════
    # 5. quota_counters — atomic period-aware quota counters
    # ══════════════════════════════════════════════════════════════════════
    op.create_table(
        "quota_counters",
        # Composite PK: tenant + type + period
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False,
                  comment="Part of composite PK"),
        sa.Column("usage_type", sa.Text(), nullable=False,
                  comment="'api_call' | 'token' — part of composite PK"),
        sa.Column("billing_period_start", sa.Date(), nullable=False,
                  comment="First day of billing month — part of composite PK (auto-scopes per period)"),
        # Counter values
        sa.Column("plan_limit", sa.BigInteger(), nullable=False,
                  comment="Plan quota limit for this type/period — denormalised for atomic UPDATE"),
        sa.Column("used", sa.BigInteger(), nullable=False, server_default="0",
                  comment="Units consumed — incremented atomically: UPDATE...WHERE used+qty<=plan_limit"),
        sa.PrimaryKeyConstraint(
            "tenant_id", "usage_type", "billing_period_start",
            name="pk_quota_counters",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_quota_counters_tenant_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "usage_type IN ('api_call', 'token')",
            name="chk_quota_counters_usage_type_valid",
        ),
        sa.CheckConstraint("plan_limit > 0", name="chk_quota_counters_plan_limit_positive"),
        sa.CheckConstraint("used >= 0", name="chk_quota_counters_used_non_negative"),
        sa.CheckConstraint("used <= plan_limit", name="chk_quota_counters_used_lte_limit"),
        comment=(
            "Period-aware quota counters. "
            "MUST use atomic UPDATE...WHERE for quota check — never SELECT+compare."
        ),
    )
    op.create_index("ix_quota_counters_tenant_type_period", "quota_counters",
                    ["tenant_id", "usage_type", "billing_period_start"])

    # ══════════════════════════════════════════════════════════════════════
    # 6. webhook_events — Stripe webhook deduplication + retry tracking
    # ══════════════════════════════════════════════════════════════════════
    op.create_table(
        "webhook_events",
        sa.Column("stripe_event_id", sa.Text(), nullable=False,
                  comment="Stripe event ID — globally unique, used for deduplication"),
        sa.Column("event_type", sa.Text(), nullable=False,
                  comment="Stripe event type e.g. 'customer.subscription.updated'"),
        # Three-state status machine (Scenario 12)
        sa.Column("status", sa.Text(), nullable=False, server_default="processing",
                  comment="processing | processed | failed"),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now(),
                  comment="When processing began — used to detect stale locks (>5min → retry)"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True,
                  comment="When processing completed; null if not done"),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        # Scenario 10: Stripe event created timestamp for order comparison
        sa.Column("stripe_event_created", sa.DateTime(timezone=True), nullable=True,
                  comment="Stripe event created time — used for out-of-order detection"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("stripe_event_id", name="pk_webhook_events"),
        sa.CheckConstraint(
            "status IN ('processing', 'processed', 'failed')",
            name="chk_webhook_events_status_valid",
        ),
        sa.CheckConstraint("retry_count >= 0", name="chk_webhook_events_retry_count_non_negative"),
        comment="Records every received Stripe webhook event. PK prevents duplicate processing.",
    )
    op.create_index("ix_webhook_events_status", "webhook_events", ["status"])
    op.create_index("ix_webhook_events_status_started", "webhook_events",
                    ["status", "processing_started_at"])

    # ══════════════════════════════════════════════════════════════════════
    # 7. monthly_usage_rollups — idempotent monthly aggregates
    # ══════════════════════════════════════════════════════════════════════
    op.create_table(
        "monthly_usage_rollups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("billing_period_start", sa.Date(), nullable=False,
                  comment="First day of billing month e.g. 2026-01-01"),
        sa.Column("usage_type", sa.Text(), nullable=False,
                  comment="'api_call' | 'token'"),
        sa.Column("total_quantity", sa.BigInteger(), nullable=False, server_default="0",
                  comment="SUM(quantity) from usage_events"),
        sa.Column("total_cost_micro_cents", sa.BigInteger(), nullable=False, server_default="0",
                  comment="SUM(cost_micro_cents) — BIGINT, never float"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False,
                  comment="When the rollup was last computed"),
        sa.PrimaryKeyConstraint("id", name="pk_monthly_usage_rollups"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_monthly_rollups_tenant_id",
            ondelete="CASCADE",
        ),
        # Scenario 20: unique constraint enables idempotent ON CONFLICT DO UPDATE
        sa.UniqueConstraint(
            "tenant_id", "billing_period_start", "usage_type",
            name="uq_monthly_rollups_tenant_period_type",
        ),
        sa.CheckConstraint(
            "usage_type IN ('api_call', 'token')",
            name="chk_monthly_rollups_usage_type_valid",
        ),
        sa.CheckConstraint("total_quantity >= 0", name="chk_monthly_rollups_quantity_non_negative"),
        sa.CheckConstraint("total_cost_micro_cents >= 0", name="chk_monthly_rollups_cost_non_negative"),
        comment=(
            "Pre-computed monthly aggregates. "
            "Populated by idempotent upsert (ON CONFLICT DO UPDATE) — Scenario 20."
        ),
    )
    op.create_index("ix_monthly_rollups_tenant_period", "monthly_usage_rollups",
                    ["tenant_id", "billing_period_start"])

    # ══════════════════════════════════════════════════════════════════════
    # 8. job_runs — background job execution log
    # ══════════════════════════════════════════════════════════════════════
    op.create_table(
        "job_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_name", sa.Text(), nullable=False,
                  comment="Job identifier e.g. 'monthly_rollup'"),
        sa.Column("billing_period", sa.Date(), nullable=True,
                  comment="Target billing period (null for period-agnostic jobs)"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending",
                  comment="pending | running | completed | failed"),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True,
                  comment="Exception traceback on failure"),
        sa.Column("rows_processed", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_job_runs"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="chk_job_runs_status_valid",
        ),
        sa.CheckConstraint("attempt_number >= 1", name="chk_job_runs_attempt_number_positive"),
        comment="Background job execution log. Partial unique index prevents duplicate concurrent runs.",
    )
    op.create_index("ix_job_runs_job_name_status", "job_runs", ["job_name", "status"])
    op.create_index("ix_job_runs_billing_period", "job_runs", ["billing_period"])
    # Partial unique index: only one RUNNING instance of a job per period at a time
    op.create_index(
        "uix_job_runs_active_job",
        "job_runs",
        ["job_name", "billing_period"],
        unique=True,
        postgresql_where="status = 'running'",
    )

    # ══════════════════════════════════════════════════════════════════════
    # Seed data: Plans
    # ══════════════════════════════════════════════════════════════════════
    op.execute("""
        INSERT INTO plans (name, display_name, api_call_limit, token_limit, price_cents_per_month)
        VALUES
            ('free', 'Free',       1000,   100000,    0),
            ('pro',  'Pro',        100000, 10000000,  2000)
        ON CONFLICT (name) DO NOTHING;
    """)


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_index("uix_job_runs_active_job", table_name="job_runs")
    op.drop_index("ix_job_runs_billing_period", table_name="job_runs")
    op.drop_index("ix_job_runs_job_name_status", table_name="job_runs")
    op.drop_table("job_runs")

    op.drop_index("ix_monthly_rollups_tenant_period", table_name="monthly_usage_rollups")
    op.drop_table("monthly_usage_rollups")

    op.drop_index("ix_webhook_events_status_started", table_name="webhook_events")
    op.drop_index("ix_webhook_events_status", table_name="webhook_events")
    op.drop_table("webhook_events")

    op.drop_index("ix_quota_counters_tenant_type_period", table_name="quota_counters")
    op.drop_table("quota_counters")

    op.drop_index("ix_usage_events_billing_period", table_name="usage_events")
    op.drop_index("ix_usage_events_tenant_idempotency", table_name="usage_events")
    op.drop_index("ix_usage_events_tenant_period", table_name="usage_events")
    op.drop_table("usage_events")

    op.drop_index("ix_subscriptions_stripe_customer_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_stripe_subscription_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_tenant_id", table_name="subscriptions")
    op.drop_table("subscriptions")

    op.drop_index("ix_tenants_stripe_customer_id", table_name="tenants")
    op.drop_index("ix_tenants_email", table_name="tenants")
    op.drop_table("tenants")

    op.drop_index("ix_plans_name", table_name="plans")
    op.drop_table("plans")
