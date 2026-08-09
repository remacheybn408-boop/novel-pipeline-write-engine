"""Error log download endpoint.

GET /api/v1/logs/errors/download reads log_dir/app.log plus every rotated
backup (app.log.1 .. app.log.LOG_BACKUP_COUNT, matching the RotatingFileHandler
configuration), extracts ERROR/CRITICAL entries together with their traceback
continuation lines, and renders them as a Markdown report attachment. When
no errors are found the report says so instead of being empty.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response

from proseforge.api.dependencies import current_user, require_admin
from proseforge.application.auth.service import AuthUser
from proseforge.runtime.logging import LOG_BACKUP_COUNT, LOG_FILENAME
from proseforge.runtime.paths import resolve_paths
from proseforge.runtime.profile import RuntimeProfile
from proseforge.settings import Settings

router = APIRouter(prefix="/api/v1", tags=["logs"])

# 报告时间口径：产品面向中文用户，统一展示上海时间（UTC+8），不再用 UTC。
SHANGHAI = ZoneInfo("Asia/Shanghai")

# A record start line: "<timestamp> LEVEL module: message" (runtime/logging.LOG_FORMAT).
_RECORD_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (\w+) ")
_ERROR_LEVELS = {"ERROR", "CRITICAL"}


def _log_dir(request: Request) -> Path:
    """Resolved log_dir; stashed on app.state by the lifespan bootstrap."""
    paths = getattr(request.app.state, "runtime_paths", None)
    if paths is not None:
        return Path(paths.log_dir)
    settings: Settings = request.app.state.settings
    env = dict(os.environ)
    if settings.data_dir:
        env["PROSEFORGE_DATA_DIR"] = settings.data_dir
    env["PROSEFORGE_DATABASE_URL"] = settings.database_url
    return Path(resolve_paths(RuntimeProfile(settings.runtime_profile), env).log_dir)


def _read_log_lines(log_dir: Path) -> list[str]:
    """Oldest first: every rotated backup (highest index = oldest) before the
    live app.log. Missing files (nothing logged yet, fresh install) simply
    contribute nothing."""
    lines: list[str] = []
    names = [f"{LOG_FILENAME}.{index}" for index in range(LOG_BACKUP_COUNT, 0, -1)]
    names.append(LOG_FILENAME)
    for name in names:
        path = log_dir / name
        if path.is_file():
            lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return lines


def _collect_error_entries(lines: list[str]) -> list[list[str]]:
    """Group ERROR/CRITICAL records with their continuation lines (traceback
    lines do not match the record-start pattern)."""
    entries: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        match = _RECORD_START.match(line)
        if match:
            current = [line] if match.group(1) in _ERROR_LEVELS else None
            if current is not None:
                entries.append(current)
        elif current is not None:
            current.append(line)
    return entries


def _render_markdown(entries: list[list[str]]) -> str:
    generated_at = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S +08:00")
    parts = ["# 错误日志报告", "", f"- 生成时间：{generated_at}", f"- 错误条目数：{len(entries)}", ""]
    if not entries:
        parts.append("未发现 ERROR 或 CRITICAL 级别的日志记录。")
        parts.append("")
        return "\n".join(parts)
    for index, entry in enumerate(entries, start=1):
        parts.append(f"## 条目 {index}")
        parts.append("")
        parts.append("```text")
        parts.extend(entry)
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


@router.get("/logs/errors/download")
async def download_error_logs(
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
) -> Response:
    # The report contains full tracebacks (server paths) and possibly SQL
    # parameters; it is installation-wide, so restrict it to ADMIN.
    require_admin(user)
    entries = _collect_error_entries(_read_log_lines(_log_dir(request)))
    body = _render_markdown(entries)
    filename = f"proseforge-error-logs-{datetime.now(SHANGHAI):%Y%m%d}.md"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/markdown",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )
