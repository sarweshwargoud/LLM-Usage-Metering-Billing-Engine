"""
SQLAlchemy engine and session factory.

Design decisions:
- Synchronous engine and SessionLocal used by FastAPI dependency injection and background services.
- Connection pooling with pre-ping validation.
- Session factory is used via FastAPI Depends(get_db) in the API layer.
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def _build_sync_engine():
    settings = get_settings()
    return create_engine(
        settings.database_url_sync,
        echo=settings.debug,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=300,
    )


sync_engine = _build_sync_engine()

SessionLocal = sessionmaker(
    bind=sync_engine,
    class_=Session,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a transactional DB session.
    Always rolls back on exception and closes the session after request.
    """
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def get_db_context() -> Generator[Session, None, None]:
    """Context manager for background jobs and CLI scripts."""
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

