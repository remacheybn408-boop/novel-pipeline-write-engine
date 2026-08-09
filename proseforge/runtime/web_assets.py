"""前端静态资源目录定位（V15-006）。

locate_frontend_dir 按优先级查找前端构建产物（SPA dist 目录）：

1. ``PROSEFORGE_FRONTEND_DIR`` 环境变量（存在且是目录）；
2. PyInstaller 捆绑目录 ``sys._MEIPASS/frontend-dist``；
3. 可执行文件/包旁的 ``frontend-dist``（native 打包产物布局，见
   packaging/native_bundle.py：frontend-dist 与 proseforge 包同级）；
4. 仓库开发布局 ``<repo>/apps/web/dist``（从 package_file 向上两级到仓库根）。

全部未命中返回 None，由调用方降级为仅 API 模式并打印警告。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

from proseforge.runtime.paths import FRONTEND_DIR_ENV


def _resolve_if_dir(candidate: Path) -> Path | None:
    return candidate.resolve() if candidate.is_dir() else None


def locate_frontend_dir(env: Mapping[str, str], *, package_file: str = __file__) -> Path | None:
    """按优先级链定位前端 dist 目录；找不到返回 None（无副作用）。"""
    override = env.get(FRONTEND_DIR_ENV, "").strip()
    if override:
        resolved = _resolve_if_dir(Path(override))
        if resolved is not None:
            return resolved
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        resolved = _resolve_if_dir(Path(meipass) / "frontend-dist")
        if resolved is not None:
            return resolved
    root = Path(package_file).resolve().parents[2]
    for candidate in (root / "frontend-dist", root / "apps" / "web" / "dist"):
        resolved = _resolve_if_dir(candidate)
        if resolved is not None:
            return resolved
    return None
