"""
Authentication & Token Issuance endpoints.
"""
from __future__ import annotations

import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.jwt import create_access_token
from app.database import get_db
from app.models.tenant import Tenant
from app.schemas.health import TokenRequest, TokenResponse

router = APIRouter(tags=["Authentication"])


@router.post("/auth/token", response_model=TokenResponse, status_code=status.HTTP_200_OK)
def issue_tenant_token(request: TokenRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """
    Issues an access token for an existing tenant identified by email.
    In testing/development, auto-provisions a tenant if not existing.
    """
    tenant = db.execute(
        select(Tenant).where(Tenant.email == request.email.strip().lower())
    ).scalars().first()

    if tenant is None:
        # Create tenant if not found
        tenant = Tenant(
            id=uuid.uuid4(),
            name=f"Tenant ({request.email.split('@')[0]})",
            email=request.email.strip().lower(),
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

    token = create_access_token(tenant_id=tenant.id, email=tenant.email)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        tenant_id=str(tenant.id),
        plan="free",
    )
