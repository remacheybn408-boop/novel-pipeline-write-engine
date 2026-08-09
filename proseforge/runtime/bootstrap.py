"""运行时目录 bootstrap（V15-002）。

只负责确保 native profile 的本地数据目录存在；不写数据库、不跑迁移
（后续任务）。server/test profile 为 no-op。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from proseforge.runtime.paths import RuntimePaths
from proseforge.runtime.profile import RuntimeProfile


@dataclass(frozen=True)
class BootstrapResult:
    """bootstrap 结果：ensured_dirs 为已确保存在的目录，skipped 表示
    当前 profile 不需要本地目录（server/test）。"""

    profile: RuntimeProfile
    ensured_dirs: tuple[Path, ...]
    skipped: bool


def bootstrap_runtime(paths: RuntimePaths, profile: RuntimeProfile) -> BootstrapResult:
    """native 时创建 data_dir/blobs/backups/logs（parents, exist_ok）。"""
    if not isinstance(profile, RuntimeProfile):
        profile = RuntimeProfile(profile)
    if profile is not RuntimeProfile.NATIVE:
        return BootstrapResult(profile=profile, ensured_dirs=(), skipped=True)
    ensured = (paths.data_dir, paths.blob_dir, paths.backup_dir, paths.log_dir)
    for directory in ensured:
        directory.mkdir(parents=True, exist_ok=True)
    return BootstrapResult(profile=profile, ensured_dirs=ensured, skipped=False)
