"""
Pydantic schemas for /health and /auth endpoints.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = Field(default="healthy", description="Service health state")
    database: str = Field(default="connected", description="Database connectivity state")
    environment: str = Field(description="Active environment (e.g. development, testing)")


class TokenRequest(BaseModel):
    email: str = Field(description="Registered tenant email address")


class TokenResponse(BaseModel):
    access_token: str = Field(description="Cryptographically signed JWT")
    token_type: str = Field(default="bearer", description="Token type")
    tenant_id: str = Field(description="Associated tenant UUID")
    plan: str = Field(default="free", description="Current subscription plan")
