"""
Application configuration — loaded from environment variables.
Never import raw os.environ; always use this settings object.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://billing:billing_secret@localhost:5432/billing_engine",
        description="Async database URL (asyncpg driver)",
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg2://billing:billing_secret@localhost:5432/billing_engine",
        description="Sync database URL (used by Alembic only)",
    )

    # ── Application ───────────────────────────────────────────────────────
    secret_key: str = Field(
        default="CHANGE_ME",
        description="HMAC/JWT signing key — must be randomized in production",
    )
    environment: Literal["development", "testing", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"

    # ── Stripe ────────────────────────────────────────────────────────────
    stripe_secret_key: str = Field(default="sk_test_PLACEHOLDER")
    stripe_publishable_key: str = Field(default="pk_test_PLACEHOLDER")
    stripe_webhook_secret: str = Field(default="whsec_PLACEHOLDER")
    stripe_price_pro: str = Field(
        default="price_1QtestProPlan000000",
        description="Stripe Price ID for Pro subscription",
    )
    stripe_success_url: str = Field(
        default="http://localhost:3000/billing/success?session_id={CHECKOUT_SESSION_ID}",
        description="Redirect URL on successful Stripe checkout completion",
    )
    stripe_cancel_url: str = Field(
        default="http://localhost:3000/billing/cancel",
        description="Redirect URL on checkout cancellation",
    )

    # ── Plan limits ───────────────────────────────────────────────────────
    free_plan_api_call_limit: int = 1_000
    free_plan_token_limit: int = 100_000
    pro_plan_api_call_limit: int = 100_000
    pro_plan_token_limit: int = 10_000_000

    # ── Pricing — ALL VALUES ARE INTEGERS (micro-cents) ──────────────────
    # 1 micro-cent = 1/100 of a cent = 1/10,000 of a dollar
    # This gives enough precision for sub-cent per-token pricing.
    token_price_input_micro_cents_per_m: int = 150_000_000   # $0.15 / 1M tokens
    token_price_cached_input_micro_cents_per_m: int = 75_000_000   # $0.075 / 1M tokens
    token_price_output_micro_cents_per_m: int = 600_000_000  # $0.60 / 1M tokens
    api_call_price_micro_cents: int = 100                     # $0.000001 / call

    # ── Background jobs ───────────────────────────────────────────────────
    rollup_job_interval_seconds: int = 3600

    @field_validator("stripe_secret_key")
    @classmethod
    def stripe_key_must_be_test_mode(cls, v: str) -> str:
        if v != "sk_test_PLACEHOLDER" and not v.startswith("sk_test_"):
            raise ValueError(
                "STRIPE_SECRET_KEY must start with 'sk_test_' — "
                "live mode is forbidden in this application"
            )
        return v


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
