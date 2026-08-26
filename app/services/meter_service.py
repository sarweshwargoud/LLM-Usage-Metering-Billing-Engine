"""
MeterService.

Responsible for recording billable usage with strict idempotency guarantees,
payload hash validation, atomic quota enforcement, and crash/concurrency resilience.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.exceptions import (
    IdempotencyConflictError,
    InvalidUsageError,
    TenantNotFoundError,
)
from app.domain.token_usage import TokenUsage
from app.models.tenant import Tenant
from app.models.usage_event import UsageEvent
from app.services.quota_enforcer import QuotaEnforcer
from app.services.token_cost_calculator import TokenCostCalculator


class MeterService:
    """
    Core metering service.
    Coordinates idempotency checking, quota enforcement, pricing, and event recording.
    """

    def __init__(
        self,
        quota_enforcer: QuotaEnforcer | None = None,
        cost_calculator: TokenCostCalculator | None = None,
    ) -> None:
        self.quota_enforcer = quota_enforcer or QuotaEnforcer()
        self.cost_calculator = cost_calculator or TokenCostCalculator()

    @staticmethod
    def compute_payload_hash(payload: Any) -> str:
        """
        Computes a deterministic SHA-256 hash of the canonical JSON representation
        of the request payload.
        """
        canonical_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def record_usage(
        self,
        session: Session,
        tenant_id: uuid.UUID,
        usage_type: str,
        quantity: int,
        idempotency_key: str,
        request_payload: Any,
        token_usage: TokenUsage | None = None,
        billing_period_start: date | None = None,
    ) -> dict[str, Any]:
        """
        Records a billable usage event with exactly-once guarantees.

        Cases handled:
        - Case A (First request): Validates, deducts quota, calculates cost, stores event & response.
        - Case B (Exact retry): Returns existing response_payload without double-charging or quota increment.
        - Case C (Key conflict): Rejects with IdempotencyConflictError if same key has different payload.
        - Concurrency: Handles simultaneous requests on the same key without 500 errors or double charging.
        """
        if not idempotency_key or not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvalidUsageError("idempotency_key must be a non-empty string")

        if quantity < 1 or quantity > 1_000_000:
            raise InvalidUsageError(f"quantity must be between 1 and 1,000,000, got: {quantity}")

        if usage_type not in ("api_call", "token"):
            raise InvalidUsageError(f"Invalid usage_type: {usage_type}. Must be 'api_call' or 'token'")

        if usage_type == "token" and token_usage is not None:
            if token_usage.total_tokens != quantity:
                raise InvalidUsageError(
                    f"TokenUsage total ({token_usage.total_tokens}) does not match declared quantity ({quantity})"
                )

        payload_hash = self.compute_payload_hash(request_payload)
        period = billing_period_start or self.quota_enforcer.get_current_billing_period()

        # Step 1: Idempotency lookup within current session
        existing_event = session.execute(
            select(UsageEvent).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.idempotency_key == idempotency_key,
            )
        ).scalars().first()

        if existing_event is not None:
            # Case C: Same key, different payload -> 422 Conflict
            if existing_event.payload_hash != payload_hash:
                raise IdempotencyConflictError(
                    f"Idempotency key '{idempotency_key}' was already used with a different request payload"
                )
            # Case B: Exact retry -> Return cached response verbatim
            return existing_event.response_payload or {
                "id": str(existing_event.id),
                "tenant_id": str(existing_event.tenant_id),
                "usage_type": existing_event.usage_type,
                "quantity": existing_event.quantity,
                "cost_micro_cents": existing_event.cost_micro_cents,
                "billing_period_start": str(existing_event.billing_period_start),
                "is_idempotent_replay": True,
            }

        # Step 2: Calculate Cost
        if usage_type == "token" and token_usage is not None:
            cost_micro_cents = self.cost_calculator.calculate_token_cost(token_usage)
        elif usage_type == "token":
            # Default token pricing assuming 100% uncached output/input average
            cost_micro_cents = self.cost_calculator.calculate_token_cost(
                TokenUsage(input_tokens=quantity, output_tokens=0)
            )
        else:
            cost_micro_cents = self.cost_calculator.calculate_api_call_cost(quantity)

        # Step 3 & 4: Deduct Quota and Insert Event within a Savepoint
        # This guarantees that if a concurrent collision occurs during insertion,
        # rolling back the savepoint completely undoes the quota deduction from this transaction.
        nested = session.begin_nested()
        try:
            quota_result = self.quota_enforcer.check_and_deduct(
                session=session,
                tenant_id=tenant_id,
                usage_type=usage_type,
                quantity=quantity,
                billing_period_start=period,
            )

            event_id = uuid.uuid4()
            response_payload = {
                "id": str(event_id),
                "tenant_id": str(tenant_id),
                "usage_type": usage_type,
                "quantity": quantity,
                "cost_micro_cents": cost_micro_cents,
                "billing_period_start": str(period),
                "quota_used": quota_result["used"],
                "quota_limit": quota_result["plan_limit"],
                "is_idempotent_replay": False,
            }

            usage_event = UsageEvent(
                id=event_id,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                usage_type=usage_type,
                quantity=quantity,
                cost_micro_cents=cost_micro_cents,
                billing_period_start=period,
                response_payload=response_payload,
            )

            session.add(usage_event)
            session.flush()
            nested.commit()
            return response_payload
        except IntegrityError as exc:
            nested.rollback()
            # If constraint violation is due to unique idempotency key from concurrent winner:
            if "uq_usage_events_tenant_idempotency" in str(exc) or "usage_events_tenant_id_idempotency_key_key" in str(exc):
                # Fetch winning row
                winner_event = session.execute(
                    select(UsageEvent).where(
                        UsageEvent.tenant_id == tenant_id,
                        UsageEvent.idempotency_key == idempotency_key,
                    )
                ).scalars().first()

                if winner_event is not None:
                    if winner_event.payload_hash != payload_hash:
                        raise IdempotencyConflictError(
                            f"Idempotency key '{idempotency_key}' was already used with a different request payload"
                        )
                    return winner_event.response_payload or {
                        "id": str(winner_event.id),
                        "tenant_id": str(winner_event.tenant_id),
                        "usage_type": winner_event.usage_type,
                        "quantity": winner_event.quantity,
                        "cost_micro_cents": winner_event.cost_micro_cents,
                        "billing_period_start": str(winner_event.billing_period_start),
                        "is_idempotent_replay": True,
                    }
            # Re-raise any other integrity error
            raise
