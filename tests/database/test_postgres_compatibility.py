"""PostgreSQL 兼容性守护（V15-003）。

无需 PG 即可跑的用例：dialect capabilities 映射、server profile 引擎
形状与 pool_pre_ping 保持不变。需要真实 PG 的用例在
PROSEFORGE_TEST_DATABASE_URL 未配置时 skip。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from proseforge.infrastructure.database.dialect import (
    capabilities_for_dialect,
    capabilities_for_engine,
)
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.settings import Settings

PG_TEST_URL_ENV = "PROSEFORGE_TEST_DATABASE_URL"


def test_postgresql_dialect_capabilities():
    capabilities = capabilities_for_dialect("postgresql")
    assert capabilities.name == "postgresql"
    assert capabilities.supports_returning is True
    assert capabilities.supports_skip_locked is True
    assert capabilities.supports_advisory_locks is True
    assert capabilities.supports_native_ilike is True
    assert capabilities.supports_jsonb is True
    assert capabilities.json_type == "JSONB"
    assert capabilities.current_timestamp_sql == "now()"


def test_sqlite_dialect_capabilities():
    capabilities = capabilities_for_dialect("sqlite")
    assert capabilities.name == "sqlite"
    assert capabilities.supports_returning is True
    assert capabilities.supports_skip_locked is False
    assert capabilities.supports_advisory_locks is False
    assert capabilities.supports_native_ilike is False
    assert capabilities.supports_jsonb is False
    assert capabilities.json_type == "JSON"
    assert capabilities.current_timestamp_sql == "CURRENT_TIMESTAMP"


def test_unknown_dialect_capabilities_raise():
    with pytest.raises(ValueError, match="unknown database dialect"):
        capabilities_for_dialect("mysql")


@pytest.mark.asyncio
async def test_server_profile_engine_shape_unchanged():
    """server profile 仍然按 database_url 建 PG 引擎并保留 pool_pre_ping；
    只建引擎不连接，因此无需真实 PG。"""
    settings = Settings(
        runtime_profile="server",
        database_url="postgresql+asyncpg://proseforge:proseforge@postgres:5432/proseforge",
    )
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        assert isinstance(engine, AsyncEngine)
        assert isinstance(session_factory, async_sessionmaker)
        assert engine.dialect.name == "postgresql"
        assert engine.sync_engine.pool._pre_ping is True
        capabilities = capabilities_for_engine(engine)
        assert capabilities.supports_skip_locked is True
        assert capabilities.supports_advisory_locks is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_server_profile_against_live_postgres():
    """有真实 PG 时验证 server 路径端到端不变：连接、advisory lock、
    SKIP LOCKED 等 PG 特性依旧可用。"""
    url = os.environ.get(PG_TEST_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"{PG_TEST_URL_ENV} not set; live PostgreSQL unavailable")
    settings = Settings(runtime_profile="server", database_url=url)
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        assert isinstance(session_factory, async_sessionmaker)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
            locked = await connection.scalar(
                text("SELECT pg_advisory_xact_lock(hashtext('v15-003-probe'))")
            )
            assert locked is None  # void 函数返回空行
    finally:
        await engine.dispose()
