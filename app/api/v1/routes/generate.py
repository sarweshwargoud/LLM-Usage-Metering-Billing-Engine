"""
Billable Generation endpoint (/generate).
"""
from __future__ import annotations

from typing import Annotated
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, get_current_tenant
from app.database import get_db
from app.domain.token_usage import TokenUsage
from app.schemas.generate import GenerateRequest, GenerateResponse
from app.services.meter_service import MeterService

router = APIRouter(tags=["Generation"])
meter_service = MeterService()


@router.post("/generate", response_model=GenerateResponse, status_code=status.HTTP_200_OK)
def generate_text(
    request: GenerateRequest,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> GenerateResponse:
    """
    Executes a metered generation call with strict idempotency and atomic quota deduction.
    Tenant identity is strictly derived from verified JWT authentication context.
    """
    # Resolve idempotency key: prefer header, check for mismatch if both provided
    if idempotency_key_header and request.idempotency_key:
        if idempotency_key_header.strip() != request.idempotency_key.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Idempotency-Key header and request body idempotency_key mismatch",
            )

    idempotency_key = (idempotency_key_header or request.idempotency_key or "").strip()
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Idempotency-Key header is required",
        )

    # Build domain TokenUsage if applicable
    token_usage: TokenUsage | None = None
    if request.usage_type == "token":
        if request.token_usage is not None:
            token_usage = TokenUsage(
                input_tokens=request.token_usage.input_tokens,
                output_tokens=request.token_usage.output_tokens,
                cached_input_tokens=request.token_usage.cached_input_tokens,
                reasoning_tokens=request.token_usage.reasoning_tokens,
            )
        else:
            token_usage = TokenUsage(
                input_tokens=request.quantity,
                output_tokens=0,
            )

    # Construct request payload dictionary for deterministic hashing
    canonical_payload = {
        "model": request.model,
        "prompt": request.prompt,
        "usage_type": request.usage_type,
        "quantity": request.quantity,
        "token_usage": request.token_usage.model_dump() if request.token_usage else None,
        "metadata": request.metadata,
    }

    # Delegate to MeterService
    response_data = meter_service.record_usage(
        session=db,
        tenant_id=tenant.id,
        usage_type=request.usage_type,
        quantity=request.quantity,
        idempotency_key=idempotency_key.strip(),
        request_payload=canonical_payload,
        token_usage=token_usage,
    )

    db.commit()

    return GenerateResponse(
        id=str(response_data["id"]),
        tenant_id=str(response_data["tenant_id"]),
        usage_type=response_data["usage_type"],
        quantity=response_data["quantity"],
        cost_micro_cents=response_data["cost_micro_cents"],
        quota_used=response_data.get("quota_used", 0),
        quota_limit=response_data.get("quota_limit", 0),
        billing_period_start=str(response_data["billing_period_start"]),
        is_idempotent_replay=response_data.get("is_idempotent_replay", False),
        result="Generated response successfully metered",
    )
