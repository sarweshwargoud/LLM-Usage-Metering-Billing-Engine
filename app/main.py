"""
Main FastAPI Application entrypoint.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.v1 import api_v1_router
from app.config import get_settings


def create_app() -> FastAPI:
    """Factory function for FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="LLM Usage Metering & Billing Engine",
        description="Production-grade usage metering, quota enforcement, and billing engine for LLM applications.",
        version="0.1.0",
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url="/redoc" if settings.environment != "production" else None,
    )

    # Configure CORS (secure non-wildcard credentials)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    # Register error handlers
    register_exception_handlers(app)

    # Include API routers (both root and /api/v1 prefixes)
    app.include_router(api_v1_router)
    app.include_router(api_v1_router, prefix="/api/v1")

    return app


app = create_app()
