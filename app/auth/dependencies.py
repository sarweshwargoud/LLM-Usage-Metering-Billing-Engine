"""
FastAPI Authentication & Tenant Context Dependencies.
Guarantees tenant isolation: tenant identity comes strictly from verified JWT claims.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.jwt import decode_access_token
from app.database import get_db
from app.models.tenant import Tenant

# HTTP Bearer authentication scheme
security = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class TenantContext:
    """
    Authenticated tenant context injected into route handlers.
    Client cannot manipulate or spoof these attributes.
    """
    id: uuid.UUID
    email: str
    name: str


def get_current_tenant(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantContext:
    """
    FastAPI dependency extracting and validating the authenticated tenant from JWT.

    Raises:
        HTTPException(401): If token is missing, malformed, invalid, expired,
                            or tenant does not exist / is deactivated.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        payload = decode_access_token(token)
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token missing 'sub' claim",
                headers={"WWW-Authenticate": "Bearer"},
            )
        tenant_id = uuid.UUID(sub)
    except (JWTError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Look up active tenant in database
    tenant = db.execute(
        select(Tenant).where(Tenant.id == tenant_id, Tenant.is_active.is_(True))
    ).scalars().first()

    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tenant not found or account is deactivated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TenantContext(
        id=tenant.id,
        email=tenant.email,
        name=tenant.name,
    )
