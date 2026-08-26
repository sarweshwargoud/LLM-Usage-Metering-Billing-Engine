"""
Stripe Service.
Handles Stripe Customer creation/reuse, Plan -> Price ID mapping, and Checkout Session creation.
"""
from __future__ import annotations

import uuid
from typing import Any
import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.exceptions import InvalidUsageError, TenantNotFoundError
from app.models.plan import Plan
from app.models.tenant import Tenant


class StripeService:
    """
    Service integrating with Stripe APIs in Test Mode.
    """

    def __init__(self) -> None:
        settings = get_settings()
        stripe.api_key = settings.stripe_secret_key

    def get_or_create_customer(self, session: Session, tenant_id: uuid.UUID) -> str:
        """
        Retrieves existing stripe_customer_id from tenant record or creates a new
        Stripe Customer and persists the identifier.
        Uses SELECT FOR UPDATE and Stripe Idempotency Key to prevent race-condition duplicates.
        """
        tenant = session.execute(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        ).scalars().first()

        if tenant is None:
            raise TenantNotFoundError(f"Tenant {tenant_id} does not exist")

        # Reuse existing Stripe customer if already linked
        if tenant.stripe_customer_id:
            return tenant.stripe_customer_id

        # Deterministic idempotency key prevents duplicate customer creation across races/retries
        customer_idem_key = f"create_customer_{tenant_id}"

        # Create new customer in Stripe
        customer = stripe.Customer.create(
            email=tenant.email,
            name=tenant.name,
            metadata={"tenant_id": str(tenant.id)},
            idempotency_key=customer_idem_key,
        )

        tenant.stripe_customer_id = customer.id
        session.flush()

        return customer.id

    def create_checkout_session(
        self,
        session: Session,
        tenant_id: uuid.UUID,
        plan_slug: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """
        Creates a Stripe Checkout Session for subscribing to a paid plan.

        Security & Business Guarantees:
        - Validates that the plan exists and is paid (rejects free tier checkout).
        - Resolves server-pinned Stripe Price ID (never trusts client price ID).
        - Reuses the tenant's Stripe Customer record.
        - Namespaces Stripe idempotency keys by tenant_id to prevent cross-tenant key collision.
        - Attaches safe metadata (tenant_id, plan) to checkout session and subscription.
        - Does NOT falsely mark the internal subscription as active.
        """
        settings = get_settings()

        # Step 1: Validate plan
        plan = session.execute(
            select(Plan).where(Plan.name == plan_slug, Plan.is_active.is_(True))
        ).scalars().first()

        if plan is None:
            raise InvalidUsageError(f"Unknown plan: '{plan_slug}'")

        if plan.name == "free" or plan.price_cents_per_month == 0:
            raise InvalidUsageError("Free tier does not require a Stripe checkout session")

        # Step 2: Resolve Stripe Price ID
        stripe_price_id = plan.stripe_price_id or settings.stripe_price_pro
        if not stripe_price_id:
            raise InvalidUsageError(f"No Stripe Price ID configured for plan '{plan_slug}'")

        # Step 3: Get or create Stripe Customer (with row lock & idempotency)
        stripe_customer_id = self.get_or_create_customer(session, tenant_id)

        # Step 4: Deterministic Tenant-Scoped Stripe Idempotency Key
        # Crucial security fix: Always namespace by tenant_id to prevent cross-tenant key collision in Stripe
        if idempotency_key:
            stripe_idem_key = f"checkout_{tenant_id}_{plan_slug}_{idempotency_key.strip()}"
        else:
            stripe_idem_key = f"checkout_{tenant_id}_{plan_slug}"

        # Step 5: Create Stripe Checkout Session
        checkout_session = stripe.checkout.Session.create(
            customer=stripe_customer_id,
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": stripe_price_id, "quantity": 1}],
            success_url=settings.stripe_success_url,
            cancel_url=settings.stripe_cancel_url,
            metadata={
                "tenant_id": str(tenant_id),
                "plan": plan_slug,
            },
            subscription_data={
                "metadata": {
                    "tenant_id": str(tenant_id),
                    "plan": plan_slug,
                }
            },
            idempotency_key=stripe_idem_key,
        )

        checkout_url = checkout_session.url or f"https://checkout.stripe.com/pay/{checkout_session.id}"

        return {
            "checkout_url": checkout_url,
            "session_id": checkout_session.id,
            "plan": plan_slug,
            "stripe_customer_id": stripe_customer_id,
        }
