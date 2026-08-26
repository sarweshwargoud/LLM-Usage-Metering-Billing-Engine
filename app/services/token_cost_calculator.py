"""
Deterministic Token & API Cost Calculator.

Enforces:
- Exact Decimal arithmetic for all rate conversions
- ROUND_HALF_UP rounding to integer micro-cents
- Strict rejection of floats
- Proper uncached vs cached input token breakdown
- Reasoning tokens billed under output tokens without double counting
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from app.config import get_settings
from app.domain.token_usage import TokenUsage


class TokenCostCalculator:
    """
    Calculates monetary cost for token and API usage in integer micro-cents.
    1 micro-cent = 1/100 cent = 1/10,000 USD.
    """

    def __init__(
        self,
        input_price_per_m: int | None = None,
        cached_input_price_per_m: int | None = None,
        output_price_per_m: int | None = None,
        api_call_price: int | None = None,
    ) -> None:
        settings = get_settings()
        self._price_input_per_m = Decimal(
            input_price_per_m
            if input_price_per_m is not None
            else settings.token_price_input_micro_cents_per_m
        )
        self._price_cached_input_per_m = Decimal(
            cached_input_price_per_m
            if cached_input_price_per_m is not None
            else settings.token_price_cached_input_micro_cents_per_m
        )
        self._price_output_per_m = Decimal(
            output_price_per_m
            if output_price_per_m is not None
            else settings.token_price_output_micro_cents_per_m
        )
        self._price_api_call = Decimal(
            api_call_price
            if api_call_price is not None
            else settings.api_call_price_micro_cents
        )
        self._million = Decimal("1000000")

    def calculate_token_cost(self, usage: TokenUsage) -> int:
        """
        Calculates total cost in micro-cents for a given TokenUsage.

        Cost breakdown:
        1. Uncached input tokens = (input_tokens - cached_input_tokens) * input_price / 1,000,000
        2. Cached input tokens   = cached_input_tokens * cached_input_price / 1,000,000
        3. Output tokens         = output_tokens * output_price / 1,000,000
           (Note: reasoning_tokens are part of output_tokens and billed at output rate)
        """
        uncached_tokens = Decimal(usage.uncached_input_tokens)
        cached_tokens = Decimal(usage.cached_input_tokens)
        output_tokens = Decimal(usage.output_tokens)

        uncached_cost = (uncached_tokens * self._price_input_per_m) / self._million
        cached_cost = (cached_tokens * self._price_cached_input_per_m) / self._million
        output_cost = (output_tokens * self._price_output_per_m) / self._million

        total_cost_decimal = (uncached_cost + cached_cost + output_cost).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )

        return int(total_cost_decimal)

    def calculate_api_call_cost(self, call_count: int = 1) -> int:
        """
        Calculates cost in micro-cents for API calls.
        """
        if call_count < 0:
            raise ValueError(f"call_count cannot be negative: {call_count}")
        total = (Decimal(call_count) * self._price_api_call).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        return int(total)
