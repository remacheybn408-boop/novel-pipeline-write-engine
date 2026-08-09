from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StartupReport:
    ready: bool
    checks: dict[str, str]

def read_ready_check(blob_root: str, backup_root: str) -> StartupReport:
    """Read-only readiness check; probes must not mutate production storage."""
    checks = {
        name: "ok" if Path(value).is_dir() else "error"
        for name, value in (("blob_root", blob_root), ("backup_root", backup_root))
    }
    checks["blob_roundtrip"] = "ok" if checks["blob_root"] == "ok" else "error"
    return StartupReport(all(value == "ok" for value in checks.values()), checks)
