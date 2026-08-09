"""Shared pytest fixtures for ProseForge tests.

v0.8.3 测试基建（AGENTS.md#M10）。新测试优先用这里的 fixture，避免
`tempfile.mktemp` / `mkdtemp` 散落各处（见 AGENTS.md#M11）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def project_root() -> Path:
    """Repo root（D:/ProseForge）。给需要 schema.sql / packs / configs 的测试用。"""
    return _REPO_ROOT


def make_fk_engine(url: str = "sqlite+aiosqlite:///:memory:"):
    """Async sqlite engine with ``PRAGMA foreign_keys=ON``.

    The ORM declares real FK constraints (migration 0035), but sqlite only
    enforces them per-connection when this pragma is set — without it tests
    would silently tolerate orphan rows the production schema now rejects.
    """
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine
