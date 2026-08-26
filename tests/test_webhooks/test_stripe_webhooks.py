"""
Tests for Stripe Webhook Ingestion, Deduplication, and Lifecycle State Machine.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch
import stripe
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models.subscription import Subscription
from app.models.tenant import Tenant
from app.models.webhook_event import WebhookEvent


def get_test_client(db_session: Session) -> TestClient:
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


class TestStripeWebhookSignatures:
    """Tests webhook signature verification."""

    def test_missing_signature_header_returns_400(self, db_session: Session):
        client = get_test_client(db_session)
        response = client.post("/billing/webhook", content=b"{}")
        assert response.status_code == 400
        assert "Invalid webhook signature" in response.json()["detail"]

    @patch("stripe.Webhook.construct_event")
    def test_invalid_signature_returns_400(self, mock_construct: MagicMock, db_session: Session):
        mock_construct.side_effect = stripe.error.SignatureVerificationError(
            "Invalid signature", "sig_header"
        )
        client = get_test_client(db_session)
        headers = {"Stripe-Signature": "t=123,v1=bad_signature"}
        response = client.post("/billing/webhook", content=b"{}", headers=headers)
        assert response.status_code == 400
        assert "Invalid webhook signature" in response.json()["detail"]


class TestStripeWebhookLifecycle:
    """Tests deduplication and subscription lifecycle state transitions."""

    @patch("stripe.Webhook.construct_event")
    def test_subscription_created_webhook_activates_subscription(
        self, mock_construct: MagicMock, db_session: Session, tenant_a: Tenant
    ):
        tenant_a.stripe_customer_id = "cus_webhook_test_001"
        db_session.commit()

        event_payload = {
            "id": "evt_sub_created_001",
            "type": "customer.subscription.created",
            "created": int(time.time()),
            "data": {
                "object": {
                    "id": "sub_stripe_12345",
                    "customer": "cus_webhook_test_001",
                    "status": "active",
                    "items": {
                        "data": [{"price": {"id": "price_1QtestProPlan000000"}}]
                    },
                    "metadata": {"tenant_id": str(tenant_a.id)},
                    "current_period_start": int(time.time()),
                    "current_period_end": int(time.time() + 30 * 86400),
                }
            },
        }
        mock_construct.return_value = event_payload

        client = get_test_client(db_session)
        headers = {"Stripe-Signature": "t=123,v1=valid_sig"}
        response = client.post(
            "/billing/webhook",
            content=json.dumps(event_payload).encode(),
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "processed"

        # Verify DB subscription was created with active status
        sub = db_session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_a.id)
        ).scalars().first()
        assert sub is not None
        assert sub.status == "active"
        assert sub.plan == "pro"
        assert sub.stripe_subscription_id == "sub_stripe_12345"

        # Verify webhook_events row was recorded as processed
        we = db_session.execute(
            select(WebhookEvent).where(WebhookEvent.stripe_event_id == "evt_sub_created_001")
        ).scalars().first()
        assert we is not None
        assert we.status == "processed"

    @patch("stripe.Webhook.construct_event")
    def test_duplicate_webhook_is_deduplicated_and_replayed_safely(
        self, mock_construct: MagicMock, db_session: Session, tenant_a: Tenant
    ):
        tenant_a.stripe_customer_id = "cus_dedup_001"
        db_session.commit()

        event_payload = {
            "id": "evt_dedup_test_001",
            "type": "customer.subscription.created",
            "created": int(time.time()),
            "data": {
                "object": {
                    "id": "sub_dedup_123",
                    "customer": "cus_dedup_001",
                    "status": "active",
                    "metadata": {"tenant_id": str(tenant_a.id)},
                }
            },
        }
        mock_construct.return_value = event_payload

        client = get_test_client(db_session)
        headers = {"Stripe-Signature": "t=123,v1=valid_sig"}

        # First delivery -> processed
        res1 = client.post("/billing/webhook", content=json.dumps(event_payload).encode(), headers=headers)
        assert res1.status_code == 200
        assert res1.json()["status"] == "processed"

        # Second delivery (Stripe retry) -> already_processed (no-op)
        res2 = client.post("/billing/webhook", content=json.dumps(event_payload).encode(), headers=headers)
        assert res2.status_code == 200
        assert res2.json()["status"] == "already_processed"

    @patch("stripe.Webhook.construct_event")
    def test_subscription_deleted_cancels_subscription(
        self, mock_construct: MagicMock, db_session: Session, tenant_a: Tenant
    ):
        tenant_a.stripe_customer_id = "cus_cancel_001"
        db_session.commit()

        # Pre-seed active subscription
        sub = Subscription(
            tenant_id=tenant_a.id,
            stripe_subscription_id="sub_cancel_123",
            stripe_customer_id="cus_cancel_001",
            plan="pro",
            status="active",
        )
        db_session.add(sub)
        db_session.commit()

        cancel_time = int(time.time())
        event_payload = {
            "id": "evt_cancel_001",
            "type": "customer.subscription.deleted",
            "created": cancel_time,
            "data": {
                "object": {
                    "id": "sub_cancel_123",
                    "customer": "cus_cancel_001",
                    "metadata": {"tenant_id": str(tenant_a.id)},
                }
            },
        }
        mock_construct.return_value = event_payload

        client = get_test_client(db_session)
        headers = {"Stripe-Signature": "t=123,v1=valid_sig"}
        response = client.post("/billing/webhook", content=json.dumps(event_payload).encode(), headers=headers)
        assert response.status_code == 200

        # Verify DB subscription cancelled
        db_session.refresh(sub)
        assert sub.status == "cancelled"
        assert sub.cancelled_at is not None

    @patch("stripe.Webhook.construct_event")
    def test_out_of_order_stale_webhook_is_ignored(
        self, mock_construct: MagicMock, db_session: Session, tenant_a: Tenant
    ):
        tenant_a.stripe_customer_id = "cus_ooo_001"
        sub_initial = Subscription(
            tenant_id=tenant_a.id,
            stripe_subscription_id="sub_ooo_123",
            stripe_customer_id="cus_ooo_001",
            plan="pro",
            status="active",
        )
        db_session.add(sub_initial)
        db_session.commit()

        # Newer event at timestamp 2000 has already cancelled the subscription
        newer_time = 2000
        event_newer = {
            "id": "evt_newer_002",
            "type": "customer.subscription.deleted",
            "created": newer_time,
            "data": {
                "object": {
                    "id": "sub_ooo_123",
                    "customer": "cus_ooo_001",
                    "metadata": {"tenant_id": str(tenant_a.id)},
                }
            },
        }
        mock_construct.return_value = event_newer
        client = get_test_client(db_session)
        headers = {"Stripe-Signature": "t=123,v1=valid_sig"}
        client.post("/billing/webhook", content=json.dumps(event_newer).encode(), headers=headers)

        sub = db_session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_a.id)
        ).scalars().first()
        assert sub.status == "cancelled"

        # Stale event arrives with timestamp 1000 attempting to mark it active
        stale_time = 1000
        event_stale = {
            "id": "evt_stale_001",
            "type": "customer.subscription.updated",
            "created": stale_time,
            "data": {
                "object": {
                    "id": "sub_ooo_123",
                    "customer": "cus_ooo_001",
                    "status": "active",
                    "metadata": {"tenant_id": str(tenant_a.id)},
                }
            },
        }
        mock_construct.return_value = event_stale
        client.post("/billing/webhook", content=json.dumps(event_stale).encode(), headers=headers)

        # Verify status remains cancelled (stale event was ignored)
        db_session.refresh(sub)
        assert sub.status == "cancelled"

    @patch("stripe.Webhook.construct_event")
    def test_equal_timestamp_webhook_is_ignored_as_stale(
        self, mock_construct: MagicMock, db_session: Session, tenant_a: Tenant
    ):
        tenant_a.stripe_customer_id = "cus_equal_ts_001"
        db_session.commit()

        ts = 1500
        # Event 1: sets status to active at timestamp 1500
        event1 = {
            "id": "evt_equal_001",
            "type": "customer.subscription.created",
            "created": ts,
            "data": {
                "object": {
                    "id": "sub_equal_123",
                    "customer": "cus_equal_ts_001",
                    "status": "active",
                    "metadata": {"tenant_id": str(tenant_a.id)},
                }
            },
        }
        mock_construct.return_value = event1
        client = get_test_client(db_session)
        headers = {"Stripe-Signature": "t=123,v1=valid_sig"}
        client.post("/billing/webhook", content=json.dumps(event1).encode(), headers=headers)

        sub = db_session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_a.id)
        ).scalars().first()
        assert sub.status == "active"

        # Event 2: identical timestamp 1500 attempting to change status to past_due
        event2 = {
            "id": "evt_equal_002",
            "type": "customer.subscription.updated",
            "created": ts,
            "data": {
                "object": {
                    "id": "sub_equal_123",
                    "customer": "cus_equal_ts_001",
                    "status": "past_due",
                    "metadata": {"tenant_id": str(tenant_a.id)},
                }
            },
        }
        mock_construct.return_value = event2
        client.post("/billing/webhook", content=json.dumps(event2).encode(), headers=headers)

        # Invariant: equal timestamp must NOT overwrite existing status
        db_session.refresh(sub)
        assert sub.status == "active"

    @patch("stripe.Webhook.construct_event")
    def test_tenant_customer_mismatch_rejected(
        self, mock_construct: MagicMock, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        tenant_a.stripe_customer_id = "cus_legit_a"
        tenant_b.stripe_customer_id = "cus_legit_b"
        db_session.commit()

        # Webhook payload contains Customer A, but spoofed metadata claiming Tenant B
        mismatch_event = {
            "id": "evt_mismatch_001",
            "type": "customer.subscription.created",
            "created": int(time.time()),
            "data": {
                "object": {
                    "id": "sub_mismatch_123",
                    "customer": "cus_legit_a",
                    "status": "active",
                    "metadata": {"tenant_id": str(tenant_b.id)},  # Mismatch!
                }
            },
        }
        mock_construct.return_value = mismatch_event
        client = get_test_client(db_session)
        headers = {"Stripe-Signature": "t=123,v1=valid_sig"}
        res = client.post("/billing/webhook", content=json.dumps(mismatch_event).encode(), headers=headers)
        assert res.status_code == 200

        # Neither tenant should have a subscription created from this mismatched payload
        sub_b = db_session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_b.id)
        ).scalars().first()
        assert sub_b is None

