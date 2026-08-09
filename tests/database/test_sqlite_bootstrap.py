"""SQLite native 引擎与 bootstrap 迁移（V15-003）。

覆盖：PRAGMA 执行与回读断言、父目录自动创建、全新 SQLite 文件上
alembic upgrade head 建成全部表、重启后数据保留且 bootstrap 幂等。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from proseforge.infrastructure.database import models  # noqa: F401  # register metadata
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.bootstrap import ensure_schema
from proseforge.infrastructure.database.dialect import capabilities_for_engine
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.sqlite import create_sqlite_engine
from proseforge.settings import Settings


def _native_settings(db_path: Path) -> Settings:
    return Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{db_path.as_posix()}",
    )


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {row[0] for row in rows}


def _column_names(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(str(db_path)) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


@pytest.mark.asyncio
async def test_create_sqlite_engine_applies_and_asserts_pragmas(tmp_path: Path):
    db_path = tmp_path / "proseforge.sqlite3"
    engine = create_sqlite_engine(db_path)
    try:
        async with engine.connect() as connection:
            journal_mode = await connection.exec_driver_sql("PRAGMA journal_mode")
            assert journal_mode.scalar_one() == "wal"
            foreign_keys = await connection.exec_driver_sql("PRAGMA foreign_keys")
            assert foreign_keys.scalar_one() == 1
            busy_timeout = await connection.exec_driver_sql("PRAGMA busy_timeout")
            assert busy_timeout.scalar_one() == 5000
            synchronous = await connection.exec_driver_sql("PRAGMA synchronous")
            assert synchronous.scalar_one() == 1  # NORMAL
    finally:
        await engine.dispose()


def test_create_sqlite_engine_creates_parent_directories(tmp_path: Path):
    db_path = tmp_path / "nested" / "data" / "proseforge.sqlite3"
    assert not db_path.parent.exists()
    create_sqlite_engine(db_path)
    assert db_path.parent.is_dir()


def test_alembic_upgrade_head_builds_full_schema_on_fresh_file(tmp_path: Path):
    db_path = tmp_path / "proseforge.sqlite3"
    created = ensure_schema(_native_settings(db_path))

    assert created, "fresh database should report created tables"
    tables = _table_names(db_path)
    assert set(Base.metadata.tables) <= tables
    assert "alembic_version" in tables

    with sqlite3.connect(str(db_path)) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version is not None
    # Compare against the live chain head instead of a hardcoded revision so
    # new migrations never stale this assertion.
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "proseforge/infrastructure/database/migrations"))
    assert version[0] == script.get_current_head()

    # 0007 恢复的 status 列与 0003 的 messages.status 都必须落库。
    assert "status" in _column_names(db_path, "projects")
    assert "status" in _column_names(db_path, "messages")


@pytest.mark.asyncio
async def test_restart_after_bootstrap_keeps_data(tmp_path: Path):
    db_path = tmp_path / "proseforge.sqlite3"
    settings = _native_settings(db_path)
    ensure_schema(settings)

    engine, session_factory = create_engine_and_sessionmaker(settings)
    assert capabilities_for_engine(engine).name == "sqlite"
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO projects (id, owner_id, slug, title, genre, style, language, status) "
                "VALUES ('p1', 'u1', 'demo', '重启存活', '', '', 'zh-CN', 'ACTIVE')"
            )
        )
        await session.commit()
    await engine.dispose()

    # 第二次 bootstrap 是幂等 no-op：不新建任何表，数据仍在。
    assert ensure_schema(settings) == []

    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        async with session_factory() as session:
            title = await session.scalar(text("SELECT title FROM projects WHERE id = 'p1'"))
        assert title == "重启存活"
    finally:
        await engine.dispose()
