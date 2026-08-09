"""数据库方言能力描述（V15-003 native runtime）。

repository / unit-of-work 代码通过这些能力对象判断当前引擎支持的 SQL
特性（例如 PG 的 advisory lock、SKIP LOCKED），禁止检查 URL 字符串。
能力一律从 SQLAlchemy dialect 名称派生。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DialectCapabilities:
    """单一数据库方言的特性能力。"""

    name: str
    supports_returning: bool
    supports_skip_locked: bool
    supports_advisory_locks: bool
    supports_native_ilike: bool
    supports_jsonb: bool
    json_type: str
    current_timestamp_sql: str


_POSTGRESQL = DialectCapabilities(
    name="postgresql",
    supports_returning=True,
    supports_skip_locked=True,
    supports_advisory_locks=True,
    supports_native_ilike=True,
    supports_jsonb=True,
    json_type="JSONB",
    current_timestamp_sql="now()",
)

_SQLITE = DialectCapabilities(
    name="sqlite",
    supports_returning=True,
    supports_skip_locked=False,
    supports_advisory_locks=False,
    supports_native_ilike=False,
    supports_jsonb=False,
    json_type="JSON",
    current_timestamp_sql="CURRENT_TIMESTAMP",
)

_BY_NAME = {capabilities.name: capabilities for capabilities in (_POSTGRESQL, _SQLITE)}


def capabilities_for_dialect(name: str) -> DialectCapabilities:
    """按 SQLAlchemy dialect 名称返回能力；未知方言抛 ValueError。"""
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise ValueError(f"unknown database dialect: {name!r}") from exc


def capabilities_for_engine(engine) -> DialectCapabilities:
    """从（异步或同步）引擎的 dialect 派生能力。"""
    dialect = getattr(engine, "dialect", None)
    if dialect is None and hasattr(engine, "sync_engine"):
        dialect = engine.sync_engine.dialect
    if dialect is None:
        raise ValueError(f"cannot determine database dialect from {engine!r}")
    return capabilities_for_dialect(dialect.name)
