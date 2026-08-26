"""
End-to-End Billing Lifecycle Integration Test.
Validates the full journey from tenant creation to metered usage, Stripe checkout,
webhook subscription upgrade, and monthly background rollup computation.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import date
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.jwt import create_access_token
from app.database import get_db
from app.main import app
from app.models.monthly_rollup import MonthlyUsageRollup
from app.models.subscription import Subscription
from app.models.tenant import Tenant
from app.models.usage_event import UsageEvent
from app.services.job_runner import JobRunner


def get_test_client(db_session: Session) -> TestClient:
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


class TestCompleteBillingFlowE2E:
    """Comprehensive End-to-End Billing Lifecycle Integration Suite."""

    @patch("stripe.Webhook.construct_event")
    @patch("stripe.checkout.Session.create")
    @patch("stripe.Customer.create")
    def test_complete_tenant_lifecycle_from_metering_to_webhook_and_rollup(
        self,
        mock_customer_create: MagicMock,
        mock_session_create: MagicMock,
        mock_webhook_construct: MagicMock,
        db_session: Session,
    ):
        client = get_test_client(db_session)
        current_period = date.today().replace(day=1)

        # ── Step 1: Provision New Tenant ──────────────────────────────────────
        tenant_id = uuid.uuid4()
        tenant = Tenant(
            id=tenant_id,
            name="Acme Enterprise AI",
            email="billing@acme-enterprise.com",
            is_active=True,
        )
        db_session.add(tenant)
        db_session.commit()

        # ── Step 2: Issue Cryptographic JWT ───────────────────────────────────
        token = create_access_token(tenant_id=tenant.id, email=tenant.email)
        auth_headers = {"Authorization": f"Bearer {token}"}

        # Verify initial free tier usage state
        res_initial_usage = client.get("/usage", headers=auth_headers)
        assert res_initial_usage.status_code == 200
        data_usage = res_initial_usage.json()
        assert data_usage["plan"] == "free"
        assert data_usage["usage"]["token"]["plan_limit"] == 100_000
        assert data_usage["usage"]["token"]["used"] == 0

        # ── Step 3: Execute Billable Generation Requests ──────────────────────
        # Call 1: Token usage with cached and reasoning breakdown
        gen_body_1 = {
            "prompt": "Analyze market trends",
            "usage_type": "token",
            "token_usage": {
                "input_tokens": 2000,
                "output_tokens": 1000,
                "cached_input_tokens": 500,
                "reasoning_tokens": 200,
            },
        }
        res_gen_1 = client.post(
            "/generate",
            json=gen_body_1,
            headers={**auth_headers, "Idempotency-Key": "e2e-gen-001"},
        )
        assert res_gen_1.status_code == 200
        data_gen_1 = res_gen_1.json()
        assert data_gen_1["quantity"] == 3000
        # Cost: (1500 * 150) + (500 * 75) + (1000 * 600) = 225,000 + 37,500 + 600,000 = 862,500
        assert data_gen_1["cost_micro_cents"] == 862_500
        assert data_gen_1["quota_used"] == 3000

        # Call 2: API call usage
        gen_body_2 = {
            "usage_type": "api_call",
            "quantity": 10,
        }
        res_gen_2 = client.post(
            "/generate",
            json=gen_body_2,
            headers={**auth_headers, "Idempotency-Key": "e2e-gen-002"},
        )
        assert res_gen_2.status_code == 200
        assert res_gen_2.json()["cost_micro_cents"] == 1000  # 10 * 100

        # ── Step 4: Initiate Stripe Checkout to Upgrade to Pro ────────────────
        mock_customer_create.return_value = MagicMock(id="cus_acme_stripe_e2e")
        mock_session_create.return_value = MagicMock(
            id="cs_acme_session_e2e",
            url="https://checkout.stripe.com/pay/cs_acme_session_e2e",
        )

        res_checkout = client.post(
            "/billing/checkout",
            json={"plan": "pro"},
            headers={**auth_headers, "Idempotency-Key": "e2e-checkout-001"},
        )
        assert res_checkout.status_code == 200
        data_checkout = res_checkout.json()
        assert data_checkout["session_id"] == "cs_acme_session_e2e"
        assert data_checkout["stripe_customer_id"] == "cus_acme_stripe_e2e"

        # Verify customer was linked in DB, but subscription is NOT yet active
        db_session.refresh(tenant)
        assert tenant.stripe_customer_id == "cus_acme_stripe_e2e"
        active_sub_pre = db_session.execute(
            select(Subscription).where(
                Subscription.tenant_id == tenant.id,
                Subscription.status == "active",
            )
        ).scalars().first()
        assert active_sub_pre is None

        # ── Step 5: Process Stripe Webhook to Activate Pro Subscription ───────
        sub_webhook_payload = {
            "id": "evt_e2e_sub_created_999",
            "type": "customer.subscription.created",
            "created": int(time.time()),
            "data": {
                "object": {
                    "id": "sub_stripe_acme_pro",
                    "customer": "cus_acme_stripe_e2e",
                    "status": "active",
                    "items": {
                        "data": [{"price": {"id": "price_1QtestProPlan000000"}}]
                    },
                    "metadata": {"tenant_id": str(tenant.id)},
                    "current_period_start": int(time.time()),
                    "current_period_end": int(time.time() + 30 * 86400),
                }
            },
        }
        mock_webhook_construct.return_value = sub_webhook_payload

        res_webhook = client.post(
            "/billing/webhook",
            content=json.dumps(sub_webhook_payload).encode(),
            headers={"Stripe-Signature": "t=123,v1=valid_sig"},
        )
        assert res_webhook.status_code == 200
        assert res_webhook.json()["status"] == "processed"

        # Verify subscription is now active in DB and reflected in /usage
        sub_active = db_session.execute(
            select(Subscription).where(
                Subscription.tenant_id == tenant.id,
                Subscription.status == "active",
            )
        ).scalars().first()
        assert sub_active is not None
        assert sub_active.plan == "pro"
        assert sub_active.stripe_subscription_id == "sub_stripe_acme_pro"

        res_usage_upgraded = client.get("/usage", headers=auth_headers)
        assert res_usage_upgraded.status_code == 200
        data_usage_upgraded = res_usage_upgraded.json()
        assert data_usage_upgraded["plan"] == "pro"
        assert data_usage_upgraded["usage"]["token"]["used"] == 3000
        assert data_usage_upgraded["usage"]["token"]["total_cost_micro_cents"] == 862_500
        assert data_usage_upgraded["usage"]["api_call"]["used"] == 10
        assert data_usage_upgraded["usage"]["api_call"]["total_cost_micro_cents"] == 1000

        # ── Step 6: Background Monthly Rollup Job ──────────────────────────────
        runner = JobRunner()
        rollup_res = runner.run_monthly_rollup_job(db_session, current_period)
        assert rollup_res["status"] == "completed"
        assert rollup_res["rows_processed"] >= 2  # (token, api_call for this tenant plus existing test rows)

        # Verify monthly_usage_rollups rows exist with exact aggregated totals
        rollup_token = db_session.execute(
            select(MonthlyUsageRollup).where(
                MonthlyUsageRollup.tenant_id == tenant.id,
                MonthlyUsageRollup.billing_period_start == current_period,
                MonthlyUsageRollup.usage_type == "token",
            )
        ).scalars().first()
        assert rollup_token is not None
        assert rollup_token.total_quantity == 3000
        assert rollup_token.total_cost_micro_cents == 862_500

        rollup_api = db_session.execute(
            select(MonthlyUsageRollup).where(
                MonthlyUsageRollup.tenant_id == tenant.id,
                MonthlyUsageRollup.billing_period_start == current_period,
                MonthlyUsageRollup.usage_type == "api_call",
            )
        ).scalars().first()
        assert rollup_api is not None
        assert rollup_api.total_quantity == 10
        assert rollup_api.total_cost_micro_cents == 1000
