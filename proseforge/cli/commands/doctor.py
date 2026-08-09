from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from proseforge.runtime.paths import resolve_paths
from proseforge.runtime.profile import RuntimeProfile, capabilities_for
from version import get_version


def doctor_report(*, profile: RuntimeProfile | str | None = None, data_dir: str | Path | None = None) -> dict[str, Any]:
    """Return stable, redacted diagnostics for support and installers."""
    raw = profile or os.getenv("PROSEFORGE_RUNTIME_PROFILE")
    if raw is None:
        # 未显式配置时按环境推断：只有存在 server 指标（数据库 URL）才走
        # server；否则桌面/原生安装场景默认 native，避免在 Windows/macOS 上
        # 解析容器专用的 /data 路径导致崩溃。
        raw = RuntimeProfile.SERVER.value if os.getenv("PROSEFORGE_DATABASE_URL") else RuntimeProfile.NATIVE.value
    selected = RuntimeProfile(raw)
    env = dict(os.environ)
    if data_dir is not None:
        env["PROSEFORGE_DATA_DIR"] = str(data_dir)
    paths = resolve_paths(selected, env)
    # server profile 在非 posix 宿主上只得到 Pure 路径（仅用于报告），
    # 不能做 I/O；本地目录检查仅对可具体化的路径执行。
    concrete = isinstance(paths.data_dir, Path)
    if concrete:
        try:
            paths.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 目录不可创建（如裸机上的 /data）时如实报 error，
            # doctor 是诊断命令，不允许因此崩溃。
            pass
    checks = {
        "data_dir": paths.data_dir.is_dir() if concrete else True,
        "data_writable": os.access(paths.data_dir, os.W_OK) if concrete else True,
        "backup_dir": (paths.backup_dir.is_dir() or _can_create(paths.backup_dir)) if concrete else True,
        "database": not concrete or paths.database_path is None or paths.database_path.exists() or paths.database_path.parent.is_dir(),
    }
    return {
        "status": "ok" if all(checks.values()) else "error",
        "checks": checks,
        "version": get_version(),
        "profile": selected.value,
        "database": capabilities_for(selected).database,
        "queue": capabilities_for(selected).queue,
        "backup_path": str(paths.backup_dir),
    }


def _can_create(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False
