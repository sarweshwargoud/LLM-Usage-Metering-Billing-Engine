"""
Tests for /usage and /health endpoints.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
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


class TestUsageEndpoint:
    """Tests /usage summary endpoint."""

    def test_get_usage_structure(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {"Authorization": f"Bearer {token}"}

        # Perform a metered generation first
        client.post(
            "/generate",
            json={"usage_type": "api_call", "quantity": 25},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "usage-prep-01"},
        )

        response = client.get("/usage", headers=headers)
        assert response.status_code == 200
        data = response.json()

        assert data["tenant_id"] == str(tenant_a.id)
        assert data["plan"] == "free"
        assert data["subscription_status"] == "active"
        assert "api_call" in data["usage"]
        assert "token" in data["usage"]
        assert data["usage"]["api_call"]["used"] == 25
        assert data["usage"]["api_call"]["plan_limit"] == 1000
        assert data["usage"]["api_call"]["remaining"] == 975
        assert data["usage"]["api_call"]["total_cost_micro_cents"] == 2500


class TestHealthEndpoint:
    """Tests /health endpoint without secrets exposure."""

    def test_health_check_returns_healthy(self, db_session: Session):
        client = get_test_client(db_session)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "healthy"
        assert data["database"] == "connected"
        assert "environment" in data
        # Ensure no secrets or connection strings in payload
        assert "secret" not in str(data).lower()
        assert "password" not in str(data).lower()
        assert "postgresql://" not in str(data).lower()
