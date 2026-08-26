"""
Domain and Service exceptions for LLM Usage Metering & Billing Engine.
"""
from __future__ import annotations


class MeteringError(Exception):
    """Base exception for all metering and billing domain errors."""
    pass


class InvalidUsageError(MeteringError):
    """Raised when usage parameters (tokens, quantity, etc.) violate business rules."""
    pass


class QuotaExceededError(MeteringError):
    """Raised when an operation would cause usage to exceed the active plan limit."""
    def __init__(self, message: str = "Usage quota exceeded for current billing period", detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


class IdempotencyConflictError(MeteringError):
    """
    Raised when an existing idempotency key is presented with a mismatched payload.
    Corresponds to HTTP 422 Unprocessable Entity.
    """
    def __init__(self, message: str = "Idempotency key was already used with a different request payload") -> None:
        super().__init__(message)


class TenantNotFoundError(MeteringError):
    """Raised when a referenced tenant cannot be found in the database."""
    pass
