"""
Tests for the POST /generate endpoint.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.jwt import create_access_token
from app.database import get_db
from app.main import app
from app.models.tenant import Tenant


def get_test_client(db_session: Session) -> TestClient:
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


class TestGenerateEndpoint:
    """Tests /generate endpoint functionality, idempotency, and error mappings."""

    def test_valid_token_generate_request(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "api-gen-001",
        }
        body = {
            "prompt": "Summarize this article",
            "usage_type": "token",
            "token_usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cached_input_tokens": 200,
                "reasoning_tokens": 100,
            },
        }

        response = client.post("/generate", json=body, headers=headers)
        assert response.status_code == 200
        data = response.json()

        assert data["tenant_id"] == str(tenant_a.id)
        assert data["usage_type"] == "token"
        assert data["quantity"] == 1500
        assert data["cost_micro_cents"] == 435_000
        assert data["is_idempotent_replay"] is False

    def test_missing_idempotency_key_returns_422(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {"Authorization": f"Bearer {token}"}
        body = {"prompt": "Test prompt", "usage_type": "api_call", "quantity": 1}

        response = client.post("/generate", json=body, headers=headers)
        assert response.status_code == 422
        assert "Idempotency-Key" in response.json()["detail"]

    def test_invalid_quantity_returns_422(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "bad-qty-api-01",
        }
        body = {"usage_type": "api_call", "quantity": 0}

        response = client.post("/generate", json=body, headers=headers)
        assert response.status_code == 422

    def test_idempotent_replay_same_key_returns_cached_response(
        self, db_session: Session, tenant_a: Tenant
    ):
        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "replay-test-key-01",
        }
        body = {"prompt": "Translate to Spanish", "usage_type": "api_call", "quantity": 5}

        # Request 1
        res1 = client.post("/generate", json=body, headers=headers)
        assert res1.status_code == 200
        data1 = res1.json()

        # Request 2 (Retry)
        res2 = client.post("/generate", json=body, headers=headers)
        assert res2.status_code == 200
        data2 = res2.json()

        assert data2["id"] == data1["id"]
        assert data2["quantity"] == 5
        assert data2["cost_micro_cents"] == data1["cost_micro_cents"]

    def test_same_key_different_payload_returns_422_conflict(
        self, db_session: Session, tenant_a: Tenant
    ):
        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "conflict-test-key-01",
        }
        body1 = {"prompt": "Initial prompt", "usage_type": "api_call", "quantity": 5}
        body2 = {"prompt": "TAMPERED prompt", "usage_type": "api_call", "quantity": 5}

        # Request 1
        res1 = client.post("/generate", json=body1, headers=headers)
        assert res1.status_code == 200

        # Request 2 with different payload
        res2 = client.post("/generate", json=body2, headers=headers)
        assert res2.status_code == 422
        assert res2.json()["error"] == "idempotency_conflict"

    def test_quota_exceeded_returns_429(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)

        # Free tier api_call limit is 1000. Request 1001.
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "quota-overrun-01",
        }
        body = {"usage_type": "api_call", "quantity": 1001}

        response = client.post("/generate", json=body, headers=headers)
        assert response.status_code == 429
        data = response.json()
        assert data["error"] == "quota_exceeded"
        assert "quota exceeded" in data["message"].lower()
