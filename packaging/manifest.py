from __future__ import annotations

import hashlib
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from version import get_version


def build_manifest(*, version: str | None = None, git_sha: str = "unknown", target_os: str | None = None, arch: str | None = None, python_version: str | None = None, dependency_hashes: dict[str, str] | None = None, contents: list[str] | None = None) -> dict[str, Any]:
    return {
        "version": version or get_version(),
        "git_sha": git_sha,
        "python_version": python_version or platform.python_version(),
        "target": {"os": target_os or platform.system().lower(), "arch": arch or platform.machine()},
        "build_time": datetime.now(UTC).isoformat(),
        "dependency_hashes": dependency_hashes or {},
        "contents": contents or ["proseforge", "migrations", "frontend-dist", "LICENSE"],
    }


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def hash_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
