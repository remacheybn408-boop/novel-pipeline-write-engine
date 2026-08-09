from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LegacySlot:
    root: Path
    project: dict
    database: Path


def _safe_child(root: Path, child: Path) -> bool:
    return child.resolve().is_relative_to(root.resolve())


def scan_workspace(workspace: str | Path) -> tuple[LegacySlot, ...]:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"legacy workspace does not exist: {root}")
    candidates = [root, *sorted(item for item in root.iterdir() if item.is_dir())]
    slots: list[LegacySlot] = []
    for slot_root in candidates:
        project_file = slot_root / "project.json"
        database = slot_root / "novel.db"
        if not project_file.exists() and not database.exists():
            continue
        if not project_file.is_file() or not database.is_file():
            raise ValueError(f"slot is missing project.json or novel.db: {slot_root}")
        if project_file.is_symlink() or database.is_symlink() or not _safe_child(root, project_file) or not _safe_child(root, database):
            raise ValueError(f"unsafe symlink or path escape: {slot_root}")
        try:
            project = json.loads(project_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid project.json: {project_file}") from exc
        if not isinstance(project, dict):
            raise ValueError(f"project.json must contain an object: {project_file}")  # noqa: TRY004 -- ValueError is the contract: import callers catch it for user-facing errors
        slots.append(LegacySlot(slot_root, project, database))
    if not slots:
        raise ValueError(f"no legacy slots found under {root}")
    return tuple(slots)
