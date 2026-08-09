from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from proseforge.infrastructure.database import models  # noqa: F401  # register metadata
from proseforge.infrastructure.database.base import Base

config = context.config
target_metadata = Base.metadata


def _sync_driver_url(url: str) -> str:
    """Alembic 用同步驱动执行迁移；异步 URL 降级为同步驱动（psycopg v3 / pysqlite）。"""
    return url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_driver_url(config.get_main_option("sqlalchemy.url")),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    configured_url = section.get("sqlalchemy.url")
    # Native bootstrap passes a per-file SQLite URL and must not be redirected
    # to the server URL merely because the Podman test process exports one.
    # Server containers use the configured PostgreSQL placeholder and may
    # receive their real URL from the environment.
    if configured_url and configured_url.startswith("sqlite"):
        url = configured_url
    else:
        url = (
            os.environ.get("PROSEFORGE_SYNC_DATABASE_URL")
            or os.environ.get("PROSEFORGE_DATABASE_URL")
            or configured_url
        )
    section["sqlalchemy.url"] = url
    if url:
        section["sqlalchemy.url"] = _sync_driver_url(url)
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
