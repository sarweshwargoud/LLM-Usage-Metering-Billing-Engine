"""
Stripe Webhook API route.
"""
from __future__ import annotations

import logging
import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.webhook_service import WebhookService

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Webhooks"])
webhook_service = WebhookService()


@router.post("/billing/webhook", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    db: Session = Depends(get_db),
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> dict[str, str]:
    """
    Ingests and processes incoming Stripe Webhook events.
    Verifies cryptographic HMAC signature against configured STRIPE_WEBHOOK_SECRET.
    """
    payload = await request.body()

    try:
        event = webhook_service.verify_and_construct_event(payload, stripe_signature)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        logger.warning("Stripe webhook signature verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature or payload",
        ) from exc

    try:
        result = webhook_service.process_webhook_event(db, event)
        db.commit()
        return {"status": result}
    except Exception as exc:
        db.rollback()
        logger.exception("Error handling webhook %s: %s", event.get("id"), exc)
        # Return 500 so Stripe will retry
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing webhook event",
        ) from exc
