"""
Tests for JWT Authentication and Authorization.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.jwt import create_access_token
from app.database import get_db
from app.main import app
from app.models.tenant import Tenant


def get_test_client(db_session: Session) -> TestClient:
    """Returns a TestClient with the get_db dependency overridden to the test session."""
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


class TestJWTAuthentication:
    """Tests JWT authentication rules, invalid tokens, and expiration."""

    def test_missing_token_returns_401(self, db_session: Session):
        client = get_test_client(db_session)
        response = client.get("/usage")
        assert response.status_code == 401
        assert "Missing Authorization Bearer token" in response.json()["detail"]

    def test_malformed_token_returns_401(self, db_session: Session):
        client = get_test_client(db_session)
        headers = {"Authorization": "Bearer not-a-valid-jwt-token"}
        response = client.get("/usage", headers=headers)
        assert response.status_code == 401
        assert "Invalid or expired" in response.json()["detail"]

    def test_invalid_signature_returns_401(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        # Sign with wrong secret key
        bad_token = create_access_token(
            tenant_id=tenant_a.id,
            email=tenant_a.email,
        )
        # Tamper signature
        tampered_token = bad_token[:-5] + "abcde"
        headers = {"Authorization": f"Bearer {tampered_token}"}
        response = client.get("/usage", headers=headers)
        assert response.status_code == 401

    def test_expired_token_returns_401(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        # Create token that expired 1 hour ago
        expired_token = create_access_token(
            tenant_id=tenant_a.id,
            email=tenant_a.email,
            expires_delta=timedelta(hours=-1),
        )
        headers = {"Authorization": f"Bearer {expired_token}"}
        response = client.get("/usage", headers=headers)
        assert response.status_code == 401
        assert "Invalid or expired" in response.json()["detail"]

    def test_nonexistent_tenant_token_returns_401(self, db_session: Session):
        client = get_test_client(db_session)
        fake_id = uuid.uuid4()
        token = create_access_token(tenant_id=fake_id, email="fake@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/usage", headers=headers)
        assert response.status_code == 401
        assert "Tenant not found" in response.json()["detail"]

    def test_valid_token_accepted(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/usage", headers=headers)
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(tenant_a.id)

    def test_token_issue_endpoint(self, db_session: Session):
        client = get_test_client(db_session)
        response = client.post("/auth/token", json={"email": "newuser@example.com"})
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert "tenant_id" in data
