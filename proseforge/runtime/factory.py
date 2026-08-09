"""Runtime 工厂（V15-001）。

create_runtime 只装配轻量 Runtime 数据对象（profile、capabilities、
安全的 info dict）；本任务不创建数据库引擎或队列。
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from proseforge.runtime.profile import (
    RuntimeCapabilities,
    RuntimeProfile,
    capabilities_for,
)

if TYPE_CHECKING:
    from proseforge.settings import Settings


@dataclass(frozen=True)
class Runtime:
    """当前进程的运行时描述。info 仅供 /api/v1/runtime/info 之类的
    只读端点使用，绝不包含凭据、绝对路径或主机名。"""

    profile: RuntimeProfile
    capabilities: RuntimeCapabilities
    info: dict[str, Any]


def _distribution_version() -> str:
    try:
        return metadata.version("proseforge")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def create_runtime(settings: Settings) -> Runtime:
    """根据 Settings 构建 Runtime（无副作用）。"""
    profile = RuntimeProfile(settings.runtime_profile)
    capabilities = capabilities_for(profile)
    data_dir = settings.data_dir
    info: dict[str, Any] = {
        "version": _distribution_version(),
        "profile": profile.value,
        "platform": platform.system().lower() or "unknown",
        "database": capabilities.database,
        "queue": capabilities.queue,
        "web_served_by": "api" if settings.serve_web else "external",
        "data_dir_is_absolute": bool(data_dir) and Path(data_dir).is_absolute(),
    }
    return Runtime(profile=profile, capabilities=capabilities, info=info)
