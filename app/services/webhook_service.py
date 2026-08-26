"""
Stripe Webhook Service.
Handles signature verification, idempotent event deduplication, and out-of-order resilient subscription lifecycle transitions.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.plan import Plan
from app.models.subscription import Subscription
from app.models.tenant import Tenant
from app.models.webhook_event import WebhookEvent

logger = logging.getLogger(__name__)


class WebhookService:
    """
    Service for processing incoming Stripe Webhook events.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.webhook_secret = settings.stripe_webhook_secret

    def verify_and_construct_event(self, payload: bytes, sig_header: str | None) -> stripe.Event:
        """
        Verifies Stripe-Signature header against the configured webhook secret.
        Raises ValueError or stripe.error.SignatureVerificationError on verification failure.
        """
        if not sig_header:
            raise ValueError("Missing Stripe-Signature header")
        return stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=self.webhook_secret,
        )

    def process_webhook_event(self, session: Session, event: stripe.Event) -> str:
        """
        Processes a verified Stripe webhook event with full deduplication and out-of-order protection.
        Returns: 'processed', 'already_processed', or 'in_progress'.
        """
        event_id = event["id"]
        event_type = event["type"]
        event_created_ts = event.get("created")
        event_dt = (
            datetime.fromtimestamp(event_created_ts, tz=timezone.utc)
            if event_created_ts
            else datetime.now(timezone.utc)
        )

        # ── Step 1: Idempotent Deduplication Check with Row Lock ──────────────
        webhook_rec = session.execute(
            select(WebhookEvent).where(WebhookEvent.stripe_event_id == event_id).with_for_update()
        ).scalars().first()

        now = datetime.now(timezone.utc)

        if webhook_rec is not None:
            if webhook_rec.status == "processed":
                logger.info("Webhook %s already processed. Skipping.", event_id)
                return "already_processed"
            elif webhook_rec.status == "processing":
                # Stale lock check (> 5 mins)
                age_seconds = (now - webhook_rec.processing_started_at).total_seconds()
                if age_seconds < 300:
                    logger.info("Webhook %s currently in processing by another worker. Skipping.", event_id)
                    return "in_progress"
                else:
                    logger.warning("Stale processing lock detected for webhook %s. Retrying.", event_id)
                    webhook_rec.processing_started_at = now
                    webhook_rec.retry_count += 1
            elif webhook_rec.status == "failed":
                webhook_rec.status = "processing"
                webhook_rec.processing_started_at = now
                webhook_rec.retry_count += 1
        else:
            webhook_rec = WebhookEvent(
                stripe_event_id=event_id,
                event_type=event_type,
                status="processing",
                processing_started_at=now,
                stripe_event_created=event_dt,
            )
            session.add(webhook_rec)
            session.flush()

        # ── Step 2: Handle Specific Event Types inside a Savepoint ───────────
        nested = session.begin_nested()
        try:
            data_object = event["data"]["object"]
            self._dispatch_event(session, event_type, data_object, event_dt)
            nested.commit()

            # Mark processed
            webhook_rec.status = "processed"
            webhook_rec.processed_at = datetime.now(timezone.utc)
            webhook_rec.failed_at = None
            webhook_rec.failure_reason = None
            session.flush()
            return "processed"

        except Exception as exc:
            nested.rollback()
            webhook_rec.status = "failed"
            webhook_rec.failed_at = datetime.now(timezone.utc)
            webhook_rec.failure_reason = str(exc)[:500]
            session.flush()
            raise

    def _dispatch_event(
        self, session: Session, event_type: str, data: dict[str, Any], event_dt: datetime
    ) -> None:
        """Dispatches event to appropriate handler."""
        if event_type in ("customer.subscription.created", "customer.subscription.updated"):
            self._handle_subscription_upsert(session, data, event_dt)
        elif event_type == "customer.subscription.deleted":
            self._handle_subscription_deleted(session, data, event_dt)
        elif event_type == "invoice.payment_succeeded":
            self._handle_invoice_payment_succeeded(session, data, event_dt)
        elif event_type == "invoice.payment_failed":
            self._handle_invoice_payment_failed(session, data, event_dt)
        else:
            logger.debug("Unhandled webhook event type: %s", event_type)

    def _resolve_tenant(self, session: Session, data: dict[str, Any]) -> Tenant | None:
        """
        Resolves tenant via customer ID with metadata mismatch validation.
        Prevents malicious/conflicting metadata from modifying the wrong tenant.
        """
        customer_id = data.get("customer")
        metadata = data.get("metadata") or {}
        tenant_id_str = metadata.get("tenant_id")

        if customer_id:
            tenant_by_customer = session.execute(
                select(Tenant).where(Tenant.stripe_customer_id == customer_id)
            ).scalars().first()
            if tenant_by_customer:
                # If metadata also provides tenant_id, verify they match!
                if tenant_id_str and str(tenant_by_customer.id) != tenant_id_str:
                    logger.error(
                        "Tenant mismatch in Stripe webhook: customer %s belongs to tenant %s, but metadata claims %s",
                        customer_id, tenant_by_customer.id, tenant_id_str,
                    )
                    return None
                return tenant_by_customer

        # Fallback to metadata only if customer not yet linked in DB
        if tenant_id_str:
            try:
                t_uuid = uuid.UUID(tenant_id_str)
                return session.execute(
                    select(Tenant).where(Tenant.id == t_uuid)
                ).scalars().first()
            except ValueError:
                pass

        return None

    def _handle_subscription_upsert(
        self, session: Session, data: dict[str, Any], event_dt: datetime
    ) -> None:
        sub_id = data.get("id")
        customer_id = data.get("customer")
        stripe_status = data.get("status", "active")

        # Map stripe status to internal constraint ('active', 'past_due', 'cancelled', 'trialing', 'incomplete')
        status_map = {
            "active": "active",
            "past_due": "past_due",
            "canceled": "cancelled",
            "cancelled": "cancelled",
            "trialing": "trialing",
            "incomplete": "incomplete",
            "incomplete_expired": "cancelled",
            "unpaid": "past_due",
        }
        internal_status = status_map.get(stripe_status, "active")

        # Resolve plan
        settings = get_settings()
        items = data.get("items", {}).get("data", [])
        price_id = items[0].get("price", {}).get("id") if items else None
        plan_name = "pro" if (price_id and price_id == settings.stripe_price_pro) else "pro"

        # Resolve tenant
        tenant = self._resolve_tenant(session, data)
        if tenant is None:
            logger.warning("No tenant resolved for Stripe subscription %s (customer %s)", sub_id, customer_id)
            return

        # Link customer ID if not already linked
        if not tenant.stripe_customer_id and customer_id:
            tenant.stripe_customer_id = customer_id

        # Period start / end timestamps
        current_period_start = None
        current_period_end = None
        if data.get("current_period_start"):
            current_period_start = datetime.fromtimestamp(data["current_period_start"], tz=timezone.utc)
        if data.get("current_period_end"):
            current_period_end = datetime.fromtimestamp(data["current_period_end"], tz=timezone.utc)

        # Look up existing subscription
        sub = session.execute(
            select(Subscription).where(
                (Subscription.stripe_subscription_id == sub_id) | (Subscription.tenant_id == tenant.id)
            ).order_by(Subscription.created_at.desc())
        ).scalars().first()

        # Out-of-order check (Scenario 10): event_dt <= last_event_timestamp must be skipped
        if sub is not None and sub.last_event_timestamp is not None:
            if event_dt <= sub.last_event_timestamp:
                logger.info(
                    "Skipping stale/out-of-order webhook for subscription %s (event_dt=%s <= last_applied=%s)",
                    sub_id, event_dt, sub.last_event_timestamp,
                )
                return

        if sub is None:
            sub = Subscription(
                tenant_id=tenant.id,
                stripe_subscription_id=sub_id,
                stripe_customer_id=customer_id,
                plan=plan_name,
                status=internal_status,
                current_period_start=current_period_start,
                current_period_end=current_period_end,
                last_event_timestamp=event_dt,
            )
            session.add(sub)
        else:
            sub.stripe_subscription_id = sub_id
            sub.stripe_customer_id = customer_id
            sub.plan = plan_name
            sub.status = internal_status
            sub.current_period_start = current_period_start
            sub.current_period_end = current_period_end
            sub.last_event_timestamp = event_dt

        session.flush()

    def _handle_subscription_deleted(
        self, session: Session, data: dict[str, Any], event_dt: datetime
    ) -> None:
        sub_id = data.get("id")
        sub = session.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == sub_id)
        ).scalars().first()

        if sub is None:
            tenant = self._resolve_tenant(session, data)
            if tenant:
                sub = session.execute(
                    select(Subscription).where(Subscription.tenant_id == tenant.id)
                ).scalars().first()

        if sub is None:
            return

        # Out-of-order check: event_dt <= last_event_timestamp must be skipped
        if sub.last_event_timestamp is not None and event_dt <= sub.last_event_timestamp:
            logger.info("Skipping stale/out-of-order subscription.deleted webhook for %s", sub_id)
            return

        sub.status = "cancelled"
        sub.cancelled_at = event_dt
        sub.last_event_timestamp = event_dt
        session.flush()

    def _handle_invoice_payment_succeeded(
        self, session: Session, data: dict[str, Any], event_dt: datetime
    ) -> None:
        sub_id = data.get("subscription")
        if not sub_id:
            return

        sub = session.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == sub_id)
        ).scalars().first()

        if sub and sub.status != "cancelled":
            if sub.last_event_timestamp is None or event_dt > sub.last_event_timestamp:
                sub.status = "active"
                sub.last_event_timestamp = event_dt
                session.flush()

    def _handle_invoice_payment_failed(
        self, session: Session, data: dict[str, Any], event_dt: datetime
    ) -> None:
        sub_id = data.get("subscription")
        if not sub_id:
            return

        sub = session.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == sub_id)
        ).scalars().first()

        if sub and sub.status != "cancelled":
            if sub.last_event_timestamp is None or event_dt > sub.last_event_timestamp:
                sub.status = "past_due"
                sub.last_event_timestamp = event_dt
                session.flush()
