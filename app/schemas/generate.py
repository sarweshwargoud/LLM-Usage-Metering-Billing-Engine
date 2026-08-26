"""
Pydantic schemas for the /generate endpoint.
"""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class TokenUsageSchema(BaseModel):
    input_tokens: int = Field(default=0, ge=0, description="Total input tokens")
    output_tokens: int = Field(default=0, ge=0, description="Total output tokens")
    cached_input_tokens: int = Field(default=0, ge=0, description="Cached input tokens (subset of input)")
    reasoning_tokens: int = Field(default=0, ge=0, description="Reasoning tokens (subset of output)")

    @model_validator(mode="after")
    def validate_token_invariants(self) -> TokenUsageSchema:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError(
                f"cached_input_tokens ({self.cached_input_tokens}) cannot exceed input_tokens ({self.input_tokens})"
            )
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError(
                f"reasoning_tokens ({self.reasoning_tokens}) cannot exceed output_tokens ({self.output_tokens})"
            )
        return self


class GenerateRequest(BaseModel):
    """
    Request body for billable generation call.
    Note: tenant_id is NOT accepted here to prevent client spoofing.
    """
    prompt: str = Field(default="Hello", description="Prompt text or instruction")
    model: str = Field(default="mock-llm", description="Target model identifier")
    usage_type: Literal["api_call", "token"] = Field(
        default="token", description="Type of usage: 'api_call' or 'token'"
    )
    quantity: int = Field(
        default=1, ge=1, le=1_000_000, description="Number of units consumed (1 to 1,000,000)"
    )
    token_usage: TokenUsageSchema | None = Field(
        default=None, description="Detailed breakdown if usage_type is 'token'"
    )
    idempotency_key: str | None = Field(
        default=None,
        description="Optional idempotency key in body (fallback if Idempotency-Key header is omitted)",
    )
    metadata: dict[str, Any] | None = Field(
        default=None, description="Arbitrary client metadata"
    )

    @model_validator(mode="after")
    def validate_quantity_consistency(self) -> GenerateRequest:
        if self.usage_type == "token" and self.token_usage is not None:
            total_tokens = self.token_usage.input_tokens + self.token_usage.output_tokens
            if total_tokens > 0:
                self.quantity = total_tokens
        return self


class GenerateResponse(BaseModel):
    id: str = Field(description="Unique usage event identifier")
    tenant_id: str = Field(description="Authenticated tenant ID")
    usage_type: str = Field(description="Usage category")
    quantity: int = Field(description="Units billed")
    cost_micro_cents: int = Field(description="Total cost in micro-cents")
    quota_used: int = Field(description="Cumulative quota units used this period")
    quota_limit: int = Field(description="Plan quota limit")
    billing_period_start: str = Field(description="Billing period start date (YYYY-MM-01)")
    is_idempotent_replay: bool = Field(description="True if this response was replayed from cache")
    result: str = Field(default="Generated response successfully metered", description="Mock model output")
