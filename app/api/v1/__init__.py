"""API v1 router bundle."""
from fastapi import APIRouter
from app.api.v1.routes.auth import router as auth_router
from app.api.v1.routes.billing import router as billing_router
from app.api.v1.routes.generate import router as generate_router
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.usage import router as usage_router
from app.api.v1.routes.webhook import router as webhook_router

api_v1_router = APIRouter()
api_v1_router.include_router(health_router)
api_v1_router.include_router(auth_router)
api_v1_router.include_router(billing_router)
api_v1_router.include_router(webhook_router)
api_v1_router.include_router(generate_router)
api_v1_router.include_router(usage_router)

__all__ = ["api_v1_router"]
