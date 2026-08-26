"""
Alembic environment configuration.

Uses the SYNC database URL (psycopg2) for migrations.
Models are imported from app.models so autogenerate discovers all tables.

Design decisions:
- compare_type=True: Alembic detects column type changes.
- compare_server_default=True: Alembic detects server_default changes.
- include_schemas=False: single schema (public) only.
- transaction_per_migration=True: each migration runs in its own transaction.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ── Make the project root importable ──────────────────────────────────────
# Required when running `alembic` from the project root directory.
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# ── Import all models so autogenerate can discover them ───────────────────
from app.models import Base  # noqa: E402 — must be after sys.path update

# ── Alembic Config object ─────────────────────────────────────────────────
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Target metadata ───────────────────────────────────────────────────────
target_metadata = Base.metadata


def get_url() -> str:
    """
    Return the database URL.

    Priority:
    1. DATABASE_URL_SYNC environment variable (set in .env or CI)
    2. sqlalchemy.url from alembic.ini
    """
    url = os.environ.get("DATABASE_URL_SYNC")
    if url:
        return url
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.
    Generates SQL scripts without a live database connection.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.
    Connects to the database and applies migrations directly.
    """
    # Override sqlalchemy.url in alembic.ini with our env-based URL
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # No pooling in migration scripts
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
