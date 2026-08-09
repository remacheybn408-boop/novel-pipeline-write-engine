"""Runtime profile 基座（V1.5 native runtime，V15-001）。

只提供 profile 枚举、capability 映射与 profile/数据库组合校验；
路径解析、SQLite 引擎、本地队列在后续任务实现。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal


class RuntimeProfile(str, Enum):
    NATIVE = "native"
    SERVER = "server"
    TEST = "test"


@dataclass(frozen=True)
class RuntimeCapabilities:
    database: Literal["sqlite", "postgresql", "test"]
    queue: Literal["local", "celery", "memory"]


ALLOW_NATIVE_POSTGRES_ENV = "PROSEFORGE_ALLOW_NATIVE_POSTGRES"

_CAPABILITIES: dict[RuntimeProfile, RuntimeCapabilities] = {
    RuntimeProfile.NATIVE: RuntimeCapabilities(database="sqlite", queue="local"),
    RuntimeProfile.SERVER: RuntimeCapabilities(database="postgresql", queue="celery"),
    RuntimeProfile.TEST: RuntimeCapabilities(database="test", queue="memory"),
}


def capabilities_for(profile: RuntimeProfile) -> RuntimeCapabilities:
    """返回 profile 对应的数据库/队列能力。"""
    try:
        return _CAPABILITIES[profile]
    except KeyError as exc:
        raise ValueError(f"unknown runtime profile: {profile!r}") from exc


def _is_sqlite_url(url: str) -> bool:
    return url.strip().lower().startswith("sqlite")


def _is_postgres_url(url: str) -> bool:
    lowered = url.strip().lower()
    return lowered.startswith(("postgresql", "postgres"))


def _native_postgres_allowed(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ALLOW_NATIVE_POSTGRES_ENV, "").strip().lower() == "true"


def validate_profile_database(
    profile: RuntimeProfile | str,
    database_url: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """校验 profile 与 database_url 的组合，非法时抛 ValueError。

    - server profile 绝不静默回退 SQLite；
    - native profile 拒绝 PostgreSQL URL，除非
      PROSEFORGE_ALLOW_NATIVE_POSTGRES=true；
    - 非法 profile 值抛错。
    """
    if not isinstance(profile, RuntimeProfile):
        try:
            profile = RuntimeProfile(profile)
        except ValueError as exc:
            raise ValueError(f"invalid runtime profile: {profile!r}") from exc

    if profile is RuntimeProfile.SERVER and _is_sqlite_url(database_url):
        raise ValueError(
            "server runtime profile requires a PostgreSQL database_url; "
            "refusing to silently fall back to SQLite"
        )
    if (
        profile is RuntimeProfile.NATIVE
        and _is_postgres_url(database_url)
        and not _native_postgres_allowed(environ)
    ):
        raise ValueError(
            "native runtime profile targets SQLite; refusing PostgreSQL "
            f"database_url (set {ALLOW_NATIVE_POSTGRES_ENV}=true to override)"
        )
