"""SQLite 引擎工厂（V15-003 native runtime）。

创建带 WAL / 外键 / 忙等待保障的 aiosqlite 异步引擎；每个新连接在
connect 时执行并校验 PRAGMA，校验失败直接抛错，拒绝带着错误的
持久化保障运行。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PRAGMAS: tuple[tuple[str, str, object], ...] = (
    ("journal_mode", "WAL", "wal"),
    ("foreign_keys", "ON", 1),
    ("busy_timeout", "5000", 5000),
    ("synchronous", "NORMAL", 1),
)


def sqlite_sync_url(url: str) -> str:
    """把 aiosqlite 异步 URL 降级为 pysqlite 同步 URL（alembic 用）。"""
    return url.replace("+aiosqlite", "")


def create_sqlite_engine(path: Path) -> AsyncEngine:
    """在 path 处创建 SQLite 异步引擎（自动创建父目录）。

    URL 形如 ``sqlite+aiosqlite:///{绝对路径}``。每个新连接执行并断言：
    journal_mode=WAL、foreign_keys=ON、busy_timeout=5000、synchronous=NORMAL。
    """
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{resolved.as_posix()}")

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_and_verify_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            for name, setting, expected in _PRAGMAS:
                cursor.execute(f"PRAGMA {name}={setting}")
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(f"PRAGMA {name}")
                    row = cursor.fetchone()
                actual = row[0] if row else None
                if isinstance(expected, str):
                    matches = str(actual).lower() == expected
                else:
                    matches = actual is not None and int(actual) == expected
                if not matches:
                    raise RuntimeError(
                        f"sqlite PRAGMA {name} expected {expected!r}, got {actual!r}"
                    )
        finally:
            cursor.close()

    return engine
