"""
Domain model representing strongly-typed LLM token usage.
"""
from __future__ import annotations

from dataclasses import dataclass
from app.domain.exceptions import InvalidUsageError


@dataclass(frozen=True)
class TokenUsage:
    """
    Immutable representation of tokens consumed in an LLM call.

    Invariants:
    - All token counts must be non-negative integers.
    - cached_input_tokens <= input_tokens (cached is a subset of input).
    - reasoning_tokens <= output_tokens (reasoning is a subset of output).
    """
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        for field_name, value in [
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("cached_input_tokens", self.cached_input_tokens),
            ("reasoning_tokens", self.reasoning_tokens),
        ]:
            if not isinstance(value, int) or isinstance(value, bool):
                raise InvalidUsageError(f"{field_name} must be an integer, got {type(value).__name__}")
            if value < 0:
                raise InvalidUsageError(f"{field_name} cannot be negative: {value}")

        if self.cached_input_tokens > self.input_tokens:
            raise InvalidUsageError(
                f"cached_input_tokens ({self.cached_input_tokens}) cannot exceed total input_tokens ({self.input_tokens})"
            )

        if self.reasoning_tokens > self.output_tokens:
            raise InvalidUsageError(
                f"reasoning_tokens ({self.reasoning_tokens}) cannot exceed total output_tokens ({self.output_tokens})"
            )

    @property
    def uncached_input_tokens(self) -> int:
        """Returns the non-cached portion of input tokens."""
        return self.input_tokens - self.cached_input_tokens

    @property
    def total_tokens(self) -> int:
        """
        Total quantity of tokens consumed (input + output).
        Reasoning tokens are already part of output_tokens.
        """
        return self.input_tokens + self.output_tokens
