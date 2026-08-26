"""
JWT authentication utilities for signing, decoding, and validating tokens.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from app.config import get_settings

ALGORITHM = "HS256"


def create_access_token(
    tenant_id: uuid.UUID | str,
    email: str | None = None,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """
    Creates a cryptographically signed HMAC-SHA256 JWT for a given tenant.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(hours=24))

    to_encode: dict[str, Any] = {
        "sub": str(tenant_id),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "iss": "billing-engine",
    }
    if email:
        to_encode["email"] = email
    if extra_claims:
        to_encode.update(extra_claims)

    return jwt.encode(to_encode, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str, secret_key: str | None = None) -> dict[str, Any]:
    """
    Decodes and validates signature, expiration, and issuer of a JWT token.
    Raises JWTError if invalid or expired.
    """
    settings = get_settings()
    key = secret_key or settings.secret_key
    return jwt.decode(
        token,
        key,
        algorithms=[ALGORITHM],
        issuer="billing-engine",
        options={"verify_exp": True, "verify_iss": True},
    )
