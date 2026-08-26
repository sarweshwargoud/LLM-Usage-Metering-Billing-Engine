"""
Declarative Base and shared mixins for all SQLAlchemy models.

Rules applied (@postgresql skill + failure-scenario review):
- UUIDs for all entity PKs (opaque, globally unique, safe to expose in URLs)
- DateTime(timezone=True) everywhere (never bare TIMESTAMP)
- BIGINT for all integer counters and monetary values
- TEXT for strings (no VARCHAR(n) — use CHECK constraints for length limits)
- No FLOAT / REAL / DOUBLE PRECISION for money
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


def utcnow() -> datetime:
    """Return current UTC time with timezone info."""
    return datetime.now(timezone.utc)
