"""平台数据目录解析（V15-002）。

resolve_paths 按「显式 env 覆盖 → profile 默认 → 平台默认」的优先级解析
data_dir / database / blobs / backups / logs / frontend 六类路径：

- native：默认跟随平台约定（win32 %LOCALAPPDATA%、darwin ~/Library/
  Application Support、linux ${XDG_DATA_HOME:-~/.local/share}），数据库为
  data_dir 下的 SQLite 文件；子目录默认派生自 data_dir。
- server：沿用容器约定（/data、/data/blobs、/data/backups，与 Settings
  现有 blob_root/backup_root 默认一致），不生成 SQLite 路径。
- test：必须由 env 提供 PROSEFORGE_DATA_DIR 临时目录，缺失即抛错。

所有返回路径都是绝对路径。仅在当前宿主平台解析时返回具体 Path；
为测试模拟其他平台时返回对应的 Pure 路径（仅用于断言，不可做 I/O）。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from proseforge.runtime.profile import RuntimeProfile

DATA_DIR_ENV = "PROSEFORGE_DATA_DIR"
DATABASE_URL_ENV = "PROSEFORGE_DATABASE_URL"
BLOB_ROOT_ENV = "PROSEFORGE_BLOB_ROOT"
BACKUP_ROOT_ENV = "PROSEFORGE_BACKUP_ROOT"
FRONTEND_DIR_ENV = "PROSEFORGE_FRONTEND_DIR"

_DEFAULT_DATABASE_NAME = "proseforge.sqlite3"
_SERVER_DATA_DIR = "/data"


@dataclass(frozen=True)
class RuntimePaths:
    """解析后的运行时目录。

    database_path 为 None 表示没有本地 SQLite 文件（server profile，
    或显式给出非 SQLite / 内存 DATABASE_URL）。frontend_dir 为 None
    表示未配置前端目录。
    """

    data_dir: Path
    database_path: Path | None
    blob_dir: Path
    backup_dir: Path
    log_dir: Path
    frontend_dir: Path | None


def _normalize_platform(platform: str | None) -> str:
    raw = platform if platform is not None else sys.platform
    key = raw.lower()
    if key.startswith("linux"):
        return "linux"
    if key in {"win32", "darwin"}:
        return key
    raise ValueError(f"unsupported platform: {raw!r}")


def _path_class(platform_key: str) -> type[PurePath]:
    if platform_key == "win32":
        return Path if sys.platform == "win32" else PureWindowsPath
    return Path if os.name == "posix" else PurePosixPath


def _home(env: Mapping[str, str], cls: type[PurePath], platform_key: str) -> PurePath:
    if platform_key == "win32":
        raw = env.get("USERPROFILE") or env.get("HOME") or ""
        if not raw:
            drive, tail = env.get("HOMEDRIVE"), env.get("HOMEPATH")
            raw = f"{drive}{tail}" if drive and tail else ""
    else:
        raw = env.get("HOME") or env.get("USERPROFILE") or ""
    return cls(raw) if raw else Path.home()


def _cwd(cls: type[PurePath], home: PurePath) -> PurePath:
    return Path.cwd() if cls is Path else home


def _absolute(
    cls: type[PurePath], value: str, home: PurePath, base: PurePath
) -> PurePath:
    if value.startswith("~"):
        suffix = value[1:].lstrip("/\\")
        path = home / suffix if suffix else home
    else:
        path = cls(value)
    return path if path.is_absolute() else base / path


def _platform_default_data_dir(
    env: Mapping[str, str], cls: type[PurePath], platform_key: str, home: PurePath
) -> PurePath:
    if platform_key == "win32":
        local_app_data = env.get("LOCALAPPDATA", "").strip()
        root = cls(local_app_data) if local_app_data else home / "AppData" / "Local"
    elif platform_key == "darwin":
        root = home / "Library" / "Application Support"
    else:
        xdg_data_home = env.get("XDG_DATA_HOME", "").strip()
        root = cls(xdg_data_home) if xdg_data_home else home / ".local" / "share"
    return root / "ProseForge"


def _derive_database_path(
    env: Mapping[str, str], data_dir: PurePath, cls: type[PurePath], home: PurePath
) -> PurePath | None:
    url = env.get(DATABASE_URL_ENV, "").strip()
    if not url:
        return data_dir / _DEFAULT_DATABASE_NAME
    if not url.lower().startswith("sqlite") or "://" not in url:
        return None
    rest = url.split("://", 1)[1]
    raw = rest.removeprefix("/")
    if not raw or raw == ":memory:":
        return None
    return _absolute(cls, raw, home, data_dir)


def resolve_paths(
    profile: RuntimeProfile,
    env: Mapping[str, str],
    platform: str | None = None,
) -> RuntimePaths:
    """解析 profile 对应的运行时目录（无副作用）。

    platform 为 None 时取 sys.platform；显式传入用于跨平台测试。
    """
    if not isinstance(profile, RuntimeProfile):
        profile = RuntimeProfile(profile)
    platform_key = _normalize_platform(platform)
    # server 遵循容器（posix）路径语义，与宿主平台无关。
    cls = _path_class("linux" if profile is RuntimeProfile.SERVER else platform_key)
    home = _home(env, cls, platform_key)

    data_override = env.get(DATA_DIR_ENV, "").strip()
    if profile is RuntimeProfile.TEST and not data_override:
        raise ValueError(
            f"test runtime profile requires {DATA_DIR_ENV} to point at a "
            "temporary directory"
        )
    if data_override:
        data_dir = _absolute(cls, data_override, home, _cwd(cls, home))
    elif profile is RuntimeProfile.SERVER:
        data_dir = cls(_SERVER_DATA_DIR)
    else:
        data_dir = _platform_default_data_dir(env, cls, platform_key, home)

    if profile is RuntimeProfile.SERVER:
        database_path = None
    else:
        database_path = _derive_database_path(env, data_dir, cls, home)

    blob_override = env.get(BLOB_ROOT_ENV, "").strip()
    backup_override = env.get(BACKUP_ROOT_ENV, "").strip()
    frontend_override = env.get(FRONTEND_DIR_ENV, "").strip()
    blob_dir = (
        _absolute(cls, blob_override, home, data_dir)
        if blob_override
        else data_dir / "blobs"
    )
    backup_dir = (
        _absolute(cls, backup_override, home, data_dir)
        if backup_override
        else data_dir / "backups"
    )
    frontend_dir = (
        _absolute(cls, frontend_override, home, data_dir) if frontend_override else None
    )

    return RuntimePaths(
        data_dir=data_dir,
        database_path=database_path,
        blob_dir=blob_dir,
        backup_dir=backup_dir,
        log_dir=data_dir / "logs",
        frontend_dir=frontend_dir,
    )
