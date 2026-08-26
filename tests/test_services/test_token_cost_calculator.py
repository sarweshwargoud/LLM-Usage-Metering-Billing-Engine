"""
Tests for TokenCostCalculator and TokenUsage domain invariants.
"""
from __future__ import annotations

import pytest
from decimal import Decimal

from app.domain.exceptions import InvalidUsageError
from app.domain.token_usage import TokenUsage
from app.services.token_cost_calculator import TokenCostCalculator


class TestTokenUsageDomainValidation:
    """Tests invariant rules on the TokenUsage domain model."""

    def test_valid_token_usage_creation(self):
        usage = TokenUsage(
            input_tokens=1000,
            output_tokens=500,
            cached_input_tokens=200,
            reasoning_tokens=150,
        )
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        assert usage.cached_input_tokens == 200
        assert usage.reasoning_tokens == 150
        assert usage.uncached_input_tokens == 800
        assert usage.total_tokens == 1500

    def test_cached_input_tokens_cannot_exceed_input_tokens(self):
        with pytest.raises(InvalidUsageError, match="cached_input_tokens .* cannot exceed total input_tokens"):
            TokenUsage(input_tokens=100, output_tokens=50, cached_input_tokens=101)

    def test_reasoning_tokens_cannot_exceed_output_tokens(self):
        with pytest.raises(InvalidUsageError, match="reasoning_tokens .* cannot exceed total output_tokens"):
            TokenUsage(input_tokens=100, output_tokens=50, reasoning_tokens=51)

    @pytest.mark.parametrize("field,value", [
        ("input_tokens", -1),
        ("output_tokens", -5),
        ("cached_input_tokens", -10),
        ("reasoning_tokens", -2),
    ])
    def test_negative_tokens_rejected(self, field, value):
        kwargs = {"input_tokens": 100, "output_tokens": 100, "cached_input_tokens": 0, "reasoning_tokens": 0}
        kwargs[field] = value
        with pytest.raises(InvalidUsageError, match="cannot be negative"):
            TokenUsage(**kwargs)

    def test_boolean_or_float_tokens_rejected(self):
        with pytest.raises(InvalidUsageError, match="must be an integer"):
            TokenUsage(input_tokens=True, output_tokens=100)  # type: ignore

        with pytest.raises(InvalidUsageError, match="must be an integer"):
            TokenUsage(input_tokens=100.5, output_tokens=100)  # type: ignore


class TestTokenPricingCalculations:
    """
    Tests exact pricing math against explicit reference values.
    Rates configured:
    - Input: $0.15 / 1M = 150,000,000 micro-cents / 1M = 150 micro-cents per token
    - Cached Input: $0.075 / 1M = 75,000,000 micro-cents / 1M = 75 micro-cents per token
    - Output: $0.60 / 1M = 600,000,000 micro-cents / 1M = 600 micro-cents per token
    - API call: 100 micro-cents
    """

    @pytest.fixture
    def calculator(self) -> TokenCostCalculator:
        return TokenCostCalculator(
            input_price_per_m=150_000_000,
            cached_input_price_per_m=75_000_000,
            output_price_per_m=600_000_000,
            api_call_price=100,
        )

    def test_normal_input_pricing_only(self, calculator: TokenCostCalculator):
        # 10,000 tokens * 150 micro-cents/token = 1,500,000 micro-cents ($0.0015)
        usage = TokenUsage(input_tokens=10_000, output_tokens=0)
        cost = calculator.calculate_token_cost(usage)
        assert cost == 1_500_000

    def test_cached_input_pricing(self, calculator: TokenCostCalculator):
        # 10,000 total input tokens, all cached:
        # 10,000 * 75 micro-cents = 750,000 micro-cents ($0.00075)
        usage = TokenUsage(input_tokens=10_000, output_tokens=0, cached_input_tokens=10_000)
        cost = calculator.calculate_token_cost(usage)
        assert cost == 750_000

    def test_output_pricing_only(self, calculator: TokenCostCalculator):
        # 5,000 output tokens * 600 micro-cents/token = 3,000,000 micro-cents ($0.003)
        usage = TokenUsage(input_tokens=0, output_tokens=5_000)
        cost = calculator.calculate_token_cost(usage)
        assert cost == 3_000_000

    def test_reasoning_tokens_included_in_output_rate(self, calculator: TokenCostCalculator):
        # Reasoning tokens are a subset of output tokens, billed at output rate without double billing.
        usage = TokenUsage(input_tokens=0, output_tokens=5_000, reasoning_tokens=2_000)
        cost = calculator.calculate_token_cost(usage)
        # Expected is identical to 5,000 output tokens
        assert cost == 3_000_000

    def test_combined_token_usage_calculation(self, calculator: TokenCostCalculator):
        # 10,000 input tokens (4,000 cached, 6,000 uncached) + 2,000 output tokens (500 reasoning)
        # Uncached input: 6,000 * 150 = 900,000 micro-cents
        # Cached input:   4,000 * 75  = 300,000 micro-cents
        # Output:         2,000 * 600 = 1,200,000 micro-cents
        # Total = 900,000 + 300,000 + 1,200,000 = 2,400,000 micro-cents ($0.0024)
        usage = TokenUsage(
            input_tokens=10_000,
            output_tokens=2_000,
            cached_input_tokens=4_000,
            reasoning_tokens=500,
        )
        cost = calculator.calculate_token_cost(usage)
        assert cost == 2_400_000

    def test_zero_usage_costs_zero(self, calculator: TokenCostCalculator):
        usage = TokenUsage(input_tokens=0, output_tokens=0)
        cost = calculator.calculate_token_cost(usage)
        assert cost == 0

    def test_large_token_quantity_calculation(self, calculator: TokenCostCalculator):
        # Max single request tokens: 1,000,000 (e.g. 800k input + 200k output)
        usage = TokenUsage(input_tokens=800_000, output_tokens=200_000, cached_input_tokens=200_000)
        # Uncached: 600k * 150 = 90,000,000 micro-cents ($0.09)
        # Cached:   200k * 75  = 15,000,000 micro-cents ($0.015)
        # Output:   200k * 600 = 120,000,000 micro-cents ($0.12)
        # Total = 225,000,000 micro-cents ($0.225)
        cost = calculator.calculate_token_cost(usage)
        assert cost == 225_000_000

    def test_deterministic_half_up_rounding(self):
        # Configure fractional pricing: rate of 150_000 micro-cents / 1M = 0.15 micro-cents per token
        fractional_calc = TokenCostCalculator(
            input_price_per_m=150_000,
            cached_input_price_per_m=75_000,
            output_price_per_m=600_000,
        )
        # 3 tokens uncached at rate 0.15 = 0.45 micro-cents -> rounds down to 0
        usage_down = TokenUsage(input_tokens=3, output_tokens=0)
        assert fractional_calc.calculate_token_cost(usage_down) == 0

        # 4 tokens uncached at rate 0.15 = 0.60 micro-cents -> rounds up to 1
        usage_up = TokenUsage(input_tokens=4, output_tokens=0)
        assert fractional_calc.calculate_token_cost(usage_up) == 1

        # 1 token at 0.50 rate (500,000 / 1M) = 0.50 micro-cents -> ROUND_HALF_UP rounds to 1
        half_calc = TokenCostCalculator(input_price_per_m=500_000)
        usage_half = TokenUsage(input_tokens=1, output_tokens=0)
        assert half_calc.calculate_token_cost(usage_half) == 1

    def test_api_call_cost_calculation(self, calculator: TokenCostCalculator):
        assert calculator.calculate_api_call_cost(1) == 100
        assert calculator.calculate_api_call_cost(5) == 500
        assert calculator.calculate_api_call_cost(0) == 0
        with pytest.raises(ValueError):
            calculator.calculate_api_call_cost(-1)
