"""
Tests verifying strict Tenant Isolation across API endpoints.
"""
from __future__ import annotations

import uuid
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.jwt import create_access_token
from app.database import get_db
from app.main import app
from app.models.quota_counter import QuotaCounter
from app.models.tenant import Tenant
from app.models.usage_event import UsageEvent


def get_test_client(db_session: Session) -> TestClient:
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


class TestTenantIsolation:
    """Proves cross-tenant access is impossible and client-side tenant injection is rejected."""

    def test_tenant_a_cannot_view_tenant_b_usage(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        client = get_test_client(db_session)
        token_a = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers_a = {"Authorization": f"Bearer {token_a}"}

        # Attempt to pass tenant_b's ID via query parameters
        response = client.get(f"/usage?tenant_id={tenant_b.id}", headers=headers_a)
        assert response.status_code == 200
        data = response.json()

        # The response must ONLY contain tenant_a's data
        assert data["tenant_id"] == str(tenant_a.id)
        assert data["tenant_id"] != str(tenant_b.id)

    def test_generate_request_attributes_strictly_to_jwt_tenant(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        client = get_test_client(db_session)
        token_a = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers_a = {
            "Authorization": f"Bearer {token_a}",
            "Idempotency-Key": "isolation-key-001",
        }

        # Attempt to spoof tenant_b in the request body
        spoofed_body = {
            "prompt": "Hello",
            "usage_type": "api_call",
            "quantity": 10,
            "tenant_id": str(tenant_b.id),  # Attempted spoof
        }

        response = client.post("/generate", json=spoofed_body, headers=headers_a)
        assert response.status_code == 200
        data = response.json()

        # Response must show tenant_a ID
        assert data["tenant_id"] == str(tenant_a.id)

        # Verify DB: event was recorded ONLY for tenant_a
        event_a = db_session.execute(
            select(UsageEvent).where(
                UsageEvent.tenant_id == tenant_a.id,
                UsageEvent.idempotency_key == "isolation-key-001",
            )
        ).scalars().first()
        assert event_a is not None

        # Verify DB: ZERO events recorded for tenant_b
        event_b = db_session.execute(
            select(UsageEvent).where(
                UsageEvent.tenant_id == tenant_b.id,
                UsageEvent.idempotency_key == "isolation-key-001",
            )
        ).scalars().first()
        assert event_b is None

    def test_tenant_quotas_remain_independent_via_api(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        client = get_test_client(db_session)
        token_a = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        token_b = create_access_token(tenant_id=tenant_b.id, email=tenant_b.email)

        # Tenant A generates 50 api calls
        client.post(
            "/generate",
            json={"usage_type": "api_call", "quantity": 50},
            headers={"Authorization": f"Bearer {token_a}", "Idempotency-Key": "t-a-01"},
        )

        # Tenant B generates 30 api calls
        client.post(
            "/generate",
            json={"usage_type": "api_call", "quantity": 30},
            headers={"Authorization": f"Bearer {token_b}", "Idempotency-Key": "t-b-01"},
        )

        # Check Tenant A usage
        res_a = client.get("/usage", headers={"Authorization": f"Bearer {token_a}"}).json()
        assert res_a["usage"]["api_call"]["used"] == 50

        # Check Tenant B usage
        res_b = client.get("/usage", headers={"Authorization": f"Bearer {token_b}"}).json()
        assert res_b["usage"]["api_call"]["used"] == 30
