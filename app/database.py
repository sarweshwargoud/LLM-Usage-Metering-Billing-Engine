"""
SQLAlchemy async engine and session factory.

Design decisions:
- Uses asyncpg driver for async I/O.
- Sync engine exposed only for Alembic migrations.
- Session factory is used via FastAPI Depends in the API layer (Phase 3).
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import create_engine

from app.config import get_settings


def _build_async_engine():
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.debug,
        pool_pre_ping=True,       # Re-validate connections before use
        pool_size=10,
        max_overflow=20,
        pool_recycle=300,         # Recycle connections every 5 min
    )


def _build_sync_engine():
    """Sync engine — used only by Alembic, never by the application."""
    settings = get_settings()
    return create_engine(
        settings.database_url_sync,
        echo=settings.debug,
        pool_pre_ping=True,
    )


async_engine = _build_async_engine()
sync_engine = _build_sync_engine()

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an async DB session.
    Always closes the session after the request, even on exception.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
