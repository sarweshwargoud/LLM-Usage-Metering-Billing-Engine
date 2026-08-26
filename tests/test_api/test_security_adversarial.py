"""
Focused Adversarial Security Tests for API & Authentication Layer.
Validates:
- JWT forgery & tampering attacks
- Algorithm confusion attacks ('none', asymmetric mismatch)
- Cross-tenant spoofing across Body, Query, Header vectors
- Idempotency header vs body disagreement
- Sensitive information leakage in errors
- CORS security posture
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.orm import Session

from app.auth.jwt import ALGORITHM, create_access_token
from app.database import get_db
from app.main import app
from app.models.tenant import Tenant


def get_test_client(db_session: Session) -> TestClient:
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


class TestJWTSecurityAttacks:
    """Adversarial attacks against JWT validation."""

    def test_forged_jwt_with_wrong_secret_rejected(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        # Create token signed with attacker's custom secret
        forged_token = jwt.encode(
            {"sub": str(tenant_a.id), "iss": "billing-engine"},
            "attacker-secret-key-1234567890",
            algorithm=ALGORITHM,
        )
        response = client.get("/usage", headers={"Authorization": f"Bearer {forged_token}"})
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or expired authentication token"

    def test_tampered_payload_subject_rejected(self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant):
        client = get_test_client(db_session)
        # Generate valid token for tenant_a
        valid_token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        header_b64, payload_b64, sig_b64 = valid_token.split(".")

        # Attacker tampers payload part to replace tenant_a with tenant_b
        import base64
        import json
        payload_data = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        payload_data["sub"] = str(tenant_b.id)
        tampered_payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload_data).encode()
        ).decode().rstrip("=")

        tampered_token = f"{header_b64}.{tampered_payload_b64}.{sig_b64}"
        response = client.get("/usage", headers={"Authorization": f"Bearer {tampered_token}"})
        assert response.status_code == 401

    def test_algorithm_none_attack_rejected(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        # Construct unsigned token with alg="none"
        import base64
        import json
        header_b64 = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
        payload_b64 = base64.urlsafe_b64encode(json.dumps({"sub": str(tenant_a.id), "iss": "billing-engine"}).encode()).decode().rstrip("=")
        none_token = f"{header_b64}.{payload_b64}."

        response = client.get("/usage", headers={"Authorization": f"Bearer {none_token}"})
        assert response.status_code == 401

    def test_wrong_issuer_claim_rejected(self, db_session: Session, tenant_a: Tenant):
        client = get_test_client(db_session)
        from app.config import get_settings
        settings = get_settings()
        bad_iss_token = jwt.encode(
            {"sub": str(tenant_a.id), "iss": "malicious-issuer"},
            settings.secret_key,
            algorithm=ALGORITHM,
        )
        response = client.get("/usage", headers={"Authorization": f"Bearer {bad_iss_token}"})
        assert response.status_code == 401


class TestTenantSpoofingVectors:
    """Tests all potential client-side tenant injection vectors."""

    def test_body_tenant_id_spoofing_ignored(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        client = get_test_client(db_session)
        token_a = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {
            "Authorization": f"Bearer {token_a}",
            "Idempotency-Key": "body-spoof-01",
        }
        body = {
            "prompt": "Test",
            "usage_type": "api_call",
            "quantity": 1,
            "tenant_id": str(tenant_b.id),
        }
        response = client.post("/generate", json=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(tenant_a.id)

    def test_query_param_tenant_id_spoofing_ignored(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        client = get_test_client(db_session)
        token_a = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {"Authorization": f"Bearer {token_a}"}

        response = client.get(f"/usage?tenant_id={tenant_b.id}", headers=headers)
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(tenant_a.id)

    def test_header_tenant_id_spoofing_ignored(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        client = get_test_client(db_session)
        token_a = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {
            "Authorization": f"Bearer {token_a}",
            "X-Tenant-ID": str(tenant_b.id),
            "X-Tenant": str(tenant_b.id),
        }
        response = client.get("/usage", headers=headers)
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(tenant_a.id)


class TestIdempotencyKeyAdversarial:
    """Tests idempotency key edge cases and disagreements."""

    def test_idempotency_key_header_body_mismatch_rejected(
        self, db_session: Session, tenant_a: Tenant
    ):
        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "key-in-header-111",
        }
        body = {
            "prompt": "Test",
            "usage_type": "api_call",
            "quantity": 1,
            "idempotency_key": "key-in-body-222",  # Mismatch!
        }
        response = client.post("/generate", json=body, headers=headers)
        assert response.status_code == 422
        assert "mismatch" in response.json()["detail"].lower()


class TestInformationLeakageAndCORS:
    """Ensures responses never leak secrets, SQL, or internal stack traces."""

    def test_error_responses_contain_no_internal_paths_or_stacktraces(
        self, db_session: Session, tenant_a: Tenant
    ):
        client = get_test_client(db_session)
        token = create_access_token(tenant_id=tenant_a.id, email=tenant_a.email)

        # Trigger 422 validation error
        res = client.post(
            "/generate",
            json={"quantity": -99},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "leak-test-01"},
        )
        content_str = res.text.lower()
        assert "traceback" not in content_str
        assert "file \"" not in content_str
        assert "postgresql" not in content_str
        assert "select " not in content_str
        assert "insert " not in content_str

    def test_cors_options_preflight_security(self, db_session: Session):
        client = get_test_client(db_session)
        response = client.options(
            "/generate",
            headers={
                "Origin": "http://evil-attacker.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Idempotency-Key",
            },
        )
        assert response.status_code == 200
        # Wildcard credentials must NOT be permitted
        assert response.headers.get("access-control-allow-credentials") != "true"
