"""
Unit and Integration tests for Stripe Checkout & Subscription flow.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import stripe
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.jwt import create_access_token
from app.database import get_db
from app.main import app
from app.models.subscription import Subscription
from app.models.tenant import Tenant


def get_test_client(db_session: Session) -> TestClient:
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


class TestBillingCheckout:
    """Tests Stripe Checkout creation, customer reuse, and security invariants."""

    @patch("stripe.checkout.Session.create")
    @patch("stripe.Customer.create")
    def test_authenticated_tenant_can_create_checkout(
        self, mock_customer_create: MagicMock, mock_session_create: MagicMock,
        db_session: Session, tenant_a: Tenant
    ):
        mock_customer_create.return_value = MagicMock(id="cus_test_mock_123")
        mock_session_create.return_value = MagicMock(
            id="cs_test_mock_session_456",
            url="https://checkout.stripe.com/pay/cs_test_mock_session_456",
        )

        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {"Authorization": f"Bearer {token}"}
        body = {"plan": "pro"}

        response = client.post("/billing/checkout", json=body, headers=headers)
        assert response.status_code == 200
        data = response.json()

        assert data["session_id"] == "cs_test_mock_session_456"
        assert data["checkout_url"] == "https://checkout.stripe.com/pay/cs_test_mock_session_456"
        assert data["plan"] == "pro"
        assert data["stripe_customer_id"] == "cus_test_mock_123"

        # Verify customer was created with correct metadata and idempotency key
        mock_customer_create.assert_called_once_with(
            email=tenant_a.email,
            name=tenant_a.name,
            metadata={"tenant_id": str(tenant_a.id)},
            idempotency_key=f"create_customer_{tenant_a.id}",
        )

        # Verify checkout session created with correct parameters
        mock_session_create.assert_called_once()
        _, kwargs = mock_session_create.call_args
        assert kwargs["customer"] == "cus_test_mock_123"
        assert kwargs["mode"] == "subscription"
        assert kwargs["line_items"] == [{"price": "price_1QtestProPlan000000", "quantity": 1}]
        assert kwargs["metadata"]["tenant_id"] == str(tenant_a.id)
        assert kwargs["metadata"]["plan"] == "pro"

        # Verify stripe_customer_id was saved to database
        db_session.refresh(tenant_a)
        assert tenant_a.stripe_customer_id == "cus_test_mock_123"

    def test_unauthenticated_checkout_rejected(self, db_session: Session):
        client = get_test_client(db_session)
        response = client.post("/billing/checkout", json={"plan": "pro"})
        assert response.status_code == 401

    def test_invalid_plan_rejected(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {"Authorization": f"Bearer {token}"}

        response = client.post("/billing/checkout", json={"plan": "non_existent_plan"}, headers=headers)
        assert response.status_code == 422

    @patch("stripe.checkout.Session.create")
    @patch("stripe.Customer.create")
    def test_existing_stripe_customer_is_reused(
        self, mock_customer_create: MagicMock, mock_session_create: MagicMock,
        db_session: Session, tenant_a: Tenant
    ):
        # Pre-assign stripe_customer_id to tenant
        tenant_a.stripe_customer_id = "cus_already_existing_789"
        db_session.commit()

        mock_session_create.return_value = MagicMock(
            id="cs_test_mock_session_reused",
            url="https://checkout.stripe.com/pay/cs_test_mock_session_reused",
        )

        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {"Authorization": f"Bearer {token}"}

        response = client.post("/billing/checkout", json={"plan": "pro"}, headers=headers)
        assert response.status_code == 200

        # Customer.create must NOT be called
        mock_customer_create.assert_not_called()

        # Checkout Session must use the existing customer ID
        _, kwargs = mock_session_create.call_args
        assert kwargs["customer"] == "cus_already_existing_789"

    @patch("stripe.checkout.Session.create")
    def test_checkout_does_not_falsely_activate_subscription(
        self, mock_session_create: MagicMock, db_session: Session, tenant_a: Tenant
    ):
        tenant_a.stripe_customer_id = "cus_existing_no_sub"
        db_session.commit()

        mock_session_create.return_value = MagicMock(
            id="cs_test_session_pending",
            url="https://checkout.stripe.com/pay/cs_test_session_pending",
        )

        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {"Authorization": f"Bearer {token}"}

        response = client.post("/billing/checkout", json={"plan": "pro"}, headers=headers)
        assert response.status_code == 200

        # Verify NO active subscription was created in the database
        active_sub = db_session.execute(
            select(Subscription).where(
                Subscription.tenant_id == tenant_a.id,
                Subscription.status == "active",
            )
        ).scalars().first()
        assert active_sub is None

    @patch("stripe.checkout.Session.create")
    def test_stripe_api_error_handled_safely(
        self, mock_session_create: MagicMock, db_session: Session, tenant_a: Tenant
    ):
        tenant_a.stripe_customer_id = "cus_existing_err"
        db_session.commit()

        mock_session_create.side_effect = stripe.error.APIConnectionError("Connection to Stripe failed")

        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {"Authorization": f"Bearer {token}"}

        response = client.post("/billing/checkout", json={"plan": "pro"}, headers=headers)
        assert response.status_code == 502
        data = response.json()
        assert "Payment provider error" in data["detail"]
        # Ensure secret keys are NOT in error response
        assert "sk_test" not in str(data)

    @patch("stripe.checkout.Session.create")
    def test_checkout_tenant_spoofing_prevented(
        self, mock_session_create: MagicMock, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        tenant_a.stripe_customer_id = "cus_tenant_a"
        tenant_b.stripe_customer_id = "cus_tenant_b"
        db_session.commit()

        mock_session_create.return_value = MagicMock(
            id="cs_test_mock_session_spoof",
            url="https://checkout.stripe.com/pay/cs_test_mock_session_spoof",
        )

        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {"Authorization": f"Bearer {token}"}
        # Attempt to pass tenant_b's ID
        body = {"plan": "pro", "tenant_id": str(tenant_b.id)}

        response = client.post("/billing/checkout", json=body, headers=headers)
        assert response.status_code == 200

        # Verify session was created for tenant_a's customer, not tenant_b
        _, kwargs = mock_session_create.call_args
        assert kwargs["customer"] == "cus_tenant_a"
        assert kwargs["metadata"]["tenant_id"] == str(tenant_a.id)

    @patch("stripe.checkout.Session.create")
    def test_checkout_with_custom_idempotency_header(
        self, mock_session_create: MagicMock, db_session: Session, tenant_a: Tenant
    ):
        tenant_a.stripe_customer_id = "cus_tenant_a_idem"
        db_session.commit()

        mock_session_create.return_value = MagicMock(
            id="cs_test_mock_session_idem",
            url="https://checkout.stripe.com/pay/cs_test_mock_session_idem",
        )

        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "custom-checkout-key-999",
        }

        response = client.post("/billing/checkout", json={"plan": "pro"}, headers=headers)
        assert response.status_code == 200

        _, kwargs = mock_session_create.call_args
        assert kwargs["idempotency_key"] == f"checkout_{tenant_a.id}_pro_custom-checkout-key-999"

    @patch("stripe.checkout.Session.create")
    def test_cross_tenant_identical_idempotency_key_produces_distinct_stripe_keys(
        self, mock_session_create: MagicMock, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        tenant_a.stripe_customer_id = "cus_a"
        tenant_b.stripe_customer_id = "cus_b"
        db_session.commit()

        mock_session_create.return_value = MagicMock(
            id="cs_session_distinct",
            url="https://checkout.stripe.com/pay/cs_session_distinct",
        )

        client = get_test_client(db_session)
        token_a = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        token_b = create_access_token(tenant_id=tenant_b.id, email=tenant_b.email)

        # Both tenants use the same client-side header "shared-key-123"
        client.post(
            "/billing/checkout",
            json={"plan": "pro"},
            headers={"Authorization": f"Bearer {token_a}", "Idempotency-Key": "shared-key-123"},
        )
        _, kwargs_a = mock_session_create.call_args

        client.post(
            "/billing/checkout",
            json={"plan": "pro"},
            headers={"Authorization": f"Bearer {token_b}", "Idempotency-Key": "shared-key-123"},
        )
        _, kwargs_b = mock_session_create.call_args

        # Prove Stripe keys are isolated and distinct
        assert kwargs_a["idempotency_key"] == f"checkout_{tenant_a.id}_pro_shared-key-123"
        assert kwargs_b["idempotency_key"] == f"checkout_{tenant_b.id}_pro_shared-key-123"
        assert kwargs_a["idempotency_key"] != kwargs_b["idempotency_key"]

