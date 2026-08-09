"""Native upgrade orchestration (V15-009).

Real upgrade path behind the CLI, following the blueprint protocol:
.upgrade.lock exclusion → writable data dir check → verified backup →
record application version + alembic revision → optional queue/scheduler
stop hook → migration → health check → start hook → JSON upgrade report.

On failure the pre-upgrade files are restored from the verified backup, the
restored SQLite database (if any) is validated with PRAGMA integrity_check,
and a sanitized failure report is still written — error type names only,
never connection strings, secrets, or exception messages (which may embed
URLs with credentials).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from proseforge.operations.backup import BackupService

_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "infrastructure" / "database" / "migrations"
_DEFAULT_DATABASE_NAME = "proseforge.sqlite3"


class UpgradeBusyError(RuntimeError):
    """Another native process owns the upgrade lock."""


def alembic_migration_callable(database_url: str) -> Callable[[], None]:
    """Return a closure running ``alembic upgrade head`` against database_url.

    Uses the same migrations env.py as the alembic CLI, so behavior is
    identical: SQLite URLs are used verbatim (env.py downgrades +aiosqlite
    to the sync driver); for PostgreSQL URLs env.py may honor
    PROSEFORGE_SYNC_DATABASE_URL / PROSEFORGE_DATABASE_URL overrides.
    Running migrations on PostgreSQL is allowed — the blueprint forbids
    automatic destructive *restore* on PG, never the migration itself.
    """

    def _migrate() -> None:
        from alembic import command
        from alembic.config import Config

        config = Config()
        config.set_main_option("script_location", str(_MIGRATIONS_DIR))
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")

    return _migrate


def head_alembic_revision() -> str:
    """Return the migrations head revision without touching any database."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config).get_current_head()


def current_alembic_revision(database_url: str) -> str | None:
    """Return the alembic revision stamped in the database (None if unstamped)."""
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(_sync_url(database_url))
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def run_upgrade(
    *,
    data_dir: str | Path,
    backup_dir: str | Path,
    migrate: Callable[[], None],
    doctor: Callable[[], None] | None = None,
    start: Callable[[], None] | None = None,
    stop: Callable[[], None] | None = None,
    database_url: str | None = None,
) -> Path:
    """Execute the upgrade protocol and return the upgrade report path.

    Order: lock → writable check → backup → record version/revision →
    stop hook → migrate → doctor → start hook → report. Any failure after
    the backup restores the pre-upgrade files from the verified backup,
    checks the restored SQLite file with PRAGMA integrity_check, and
    writes a sanitized failure report before the original error re-raises.
    """
    data = Path(data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    backups = Path(backup_dir).resolve()
    lock = data / ".upgrade.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise UpgradeBusyError("another upgrade is already running") from exc
    started = datetime.now(UTC)
    try:
        handle.write("upgrade in progress\n")
        handle.close()
        if not os.access(data, os.W_OK):
            raise OSError("data directory is not writable")
        service = BackupService(backups)
        version_before = _app_version()
        revision_before = _quiet_current_revision(database_url, data)
        backup = service.create(data, application_version=version_before, migration_revision=revision_before or "unknown").archive
        report: dict[str, object] = {
            "version_before": version_before,
            "revision_before": revision_before,
            "backup": backup,
            "started_at": started.isoformat(),
        }
        try:
            if stop:
                stop()
            migrate()
            if doctor:
                doctor()
            if start:
                start()
        except Exception as exc:
            rolled_back = True
            rollback_error: str | None = None
            try:
                with tempfile.TemporaryDirectory(prefix="proseforge-rollback-") as staging:
                    service.restore(backup, staging)
                    _restore_files(Path(staging), data)
            except Exception as restore_exc:  # keep the original failure primary
                rolled_back = False
                rollback_error = type(restore_exc).__name__
            _write_report(backups, report | {
                "result": "failed",
                "version_after": _app_version(),
                "revision_after": _quiet_current_revision(database_url, data),
                "finished_at": datetime.now(UTC).isoformat(),
                "error_type": type(exc).__name__,
                "rolled_back": rolled_back,
                "rollback_error": rollback_error,
                "integrity_check": _restored_integrity(database_url, data),
            })
            raise
        return _write_report(backups, report | {
            "result": "success",
            "version_after": _app_version(),
            "revision_after": _quiet_current_revision(database_url, data),
            "finished_at": datetime.now(UTC).isoformat(),
        })
    finally:
        lock.unlink(missing_ok=True)


def check_upgrade(*, data_dir: str | Path, backup_dir: str | Path, database_url: str | None = None) -> dict[str, object]:
    """Readiness probe for the upgrade path; returns a stable-key JSON dict.

    Keys: status (ready|blocked), data_dir, backup_dir, current_revision,
    head_revision, pending, free_bytes; blocked reports also carry reason.
    """
    data = Path(data_dir)
    backups = Path(backup_dir)
    reasons: list[str] = []
    if (data / ".upgrade.lock").exists():
        reasons.append("upgrade lock held")
    if not (data.is_dir() and os.access(data, os.W_OK)):
        reasons.append("data dir missing or not writable")
    if not (backups.is_dir() or _can_create(backups)):
        reasons.append("backup dir not creatable")
    current: str | None = None
    if database_url:
        sqlite_path = _sqlite_path_from_url(database_url, data)
        try:
            # Never create a SQLite file as a side effect of a check.
            current = None if (sqlite_path is not None and not sqlite_path.exists()) else current_alembic_revision(database_url)
        except Exception as exc:
            reasons.append(f"database unreachable ({type(exc).__name__})")
    head = head_alembic_revision()
    report: dict[str, object] = {
        "status": "blocked" if reasons else "ready",
        "data_dir": str(data),
        "backup_dir": str(backups),
        "current_revision": current,
        "head_revision": head,
        "pending": current != head,
        "free_bytes": shutil.disk_usage(data).free if data.is_dir() else 0,
    }
    if reasons:
        report["reason"] = "; ".join(reasons)
    return report


def _restore_files(source: Path, destination: Path) -> None:
    for item in source.iterdir():
        if item.name == ".upgrade.lock":
            continue
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _write_report(backup_dir: Path, payload: dict[str, object]) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = backup_dir / f"upgrade-report-{stamp}.json"
    counter = 1
    while candidate.exists():
        candidate = backup_dir / f"upgrade-report-{stamp}-{counter}.json"
        counter += 1
    candidate.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return candidate


def _restored_integrity(database_url: str | None, data: Path) -> str:
    path = _sqlite_path_from_url(database_url, data)
    if path is None:
        path = data / _DEFAULT_DATABASE_NAME
    if not path.exists():
        return "skipped"
    return _sqlite_integrity_check(path)


def _sqlite_integrity_check(path: Path) -> str:
    import sqlite3

    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
    except Exception:
        return "failed"
    return "ok" if rows and all(row[0] == "ok" for row in rows) else "failed"


def _quiet_current_revision(database_url: str | None, data: Path) -> str | None:
    """Best-effort revision read for reports; never creates a SQLite file."""
    if not database_url:
        return None
    sqlite_path = _sqlite_path_from_url(database_url, data)
    if sqlite_path is not None and not sqlite_path.exists():
        return None
    try:
        return current_alembic_revision(database_url)
    except Exception:
        return None


def _sqlite_path_from_url(database_url: str | None, data_dir: Path | None = None) -> Path | None:
    """Extract the local file path from a SQLite URL (None for other schemes)."""
    if not database_url or not database_url.lower().startswith("sqlite") or "://" not in database_url:
        return None
    rest = database_url.split("://", 1)[1]
    raw = rest.removeprefix("/")
    if not raw or raw == ":memory:":
        return None
    path = Path(raw)
    if not path.is_absolute() and data_dir is not None:
        path = Path(data_dir) / path
    return path


def _sync_url(database_url: str) -> str:
    """Downgrade async drivers for the synchronous revision reader."""
    return database_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")


def _can_create(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def _app_version() -> str:
    try:
        from version import get_version

        return get_version()
    except Exception:
        return "unknown"
