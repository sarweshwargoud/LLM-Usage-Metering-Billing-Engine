"""
Billing & Stripe Checkout endpoints.
"""
from __future__ import annotations

import logging
from typing import Annotated
import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, get_current_tenant
from app.database import get_db
from app.schemas.billing import CheckoutRequest, CheckoutResponse
from app.services.stripe_service import StripeService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["Billing"])
stripe_service = StripeService()


@router.post("/checkout", response_model=CheckoutResponse, status_code=status.HTTP_200_OK)
def create_checkout_session(
    request: CheckoutRequest,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    db: Annotated[Session, Depends(get_db)],
    idempotency_key_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CheckoutResponse:
    """
    Creates a Stripe Checkout Session for the authenticated tenant to subscribe to a paid plan.

    Security guarantees:
    - Tenant identity is strictly derived from JWT context (never trusted from request body).
    - Plan resolution maps internal slug ('pro') to server-pinned Stripe Price ID.
    - Arbitrary Stripe Price IDs or Customer IDs from clients are rejected.
    - Zero false activation: database subscription is NOT activated until webhook confirmation.
    """
    try:
        result = stripe_service.create_checkout_session(
            session=db,
            tenant_id=tenant.id,
            plan_slug=request.plan,
            idempotency_key=idempotency_key_header,
        )
        db.commit()
        return CheckoutResponse(
            checkout_url=result["checkout_url"],
            session_id=result["session_id"],
            plan=result["plan"],
            stripe_customer_id=result["stripe_customer_id"],
        )
    except stripe.error.StripeError as exc:
        db.rollback()
        logger.error("Stripe API error for tenant %s: %s", tenant.id, exc.user_message or str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error while creating checkout session",
        ) from exc
