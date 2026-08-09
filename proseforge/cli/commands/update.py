"""``proseforge update`` subcommand: self-update of the native install.

Flow: fetch ``{base}/latest.json`` (base defaults to
https://proseforge.cc/proseforge/releases, overridable via the
PROSEFORGE_UPDATE_BASE env var for tests) → compare with the local
version → download the platform artifact → verify its sha256 (mismatch
aborts with exit code 2 before anything is touched) → stop the service →
extract to ``<app>.new`` → swap ``<app>`` → ``<app>.rollback`` → run the
shared migration + health check from ``operations.upgrade`` → restart the
service. Any failure after the swap restores ``<app>.rollback``.

The whole command emits a single JSON object per outcome, in the same
style as ``upgrade``; exception details are reduced to the error type
name so connection strings or credentials never leak into output.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from proseforge.cli.commands import service
from proseforge.operations.upgrade import alembic_migration_callable, run_upgrade
from proseforge.runtime.paths import resolve_paths
from proseforge.runtime.profile import RuntimeProfile

DEFAULT_UPDATE_BASE = "https://proseforge.cc/proseforge/releases"
UPDATE_BASE_ENV = "PROSEFORGE_UPDATE_BASE"
APP_DIR_ENV = "PROSEFORGE_APP_DIR"
_FETCH_TIMEOUT_SECONDS = 15.0
_DOWNLOAD_CHUNK_SIZE = 1 << 16


class ChecksumMismatchError(RuntimeError):
    """Downloaded artifact does not match the sha256 from latest.json."""


def _parse_version(value: str) -> tuple[int, ...]:
    """Parse a dotted version into an int tuple; non-digit tails are cut."""
    numbers: list[int] = []
    for segment in value.strip().lstrip("vV").split("."):
        digits = ""
        for char in segment:
            if not char.isdigit():
                break
            digits += char
        numbers.append(int(digits) if digits else 0)
    return tuple(numbers)


def is_newer(latest: str, current: str) -> bool:
    """True when ``latest`` is strictly newer than ``current``."""
    new = _parse_version(latest)
    old = _parse_version(current)
    width = max(len(new), len(old))
    new += (0,) * (width - len(new))
    old += (0,) * (width - len(old))
    return new > old


def _platform_key() -> str:
    return "windows" if os.name == "nt" else "linux"


def fetch_latest(base_url: str) -> dict[str, Any]:
    """Fetch and parse latest.json from the release feed."""
    url = f"{base_url.rstrip('/')}/latest.json"
    with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _download(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out, length=_DOWNLOAD_CHUNK_SIZE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_verified(artifact: dict[str, Any], staging: Path) -> Path:
    """Download the artifact into staging and verify its sha256."""
    url = str(artifact["url"])
    expected = str(artifact["sha256"]).strip().lower()
    name = Path(urllib.request.urlparse(url).path).name or "artifact"
    target = staging / name
    _download(url, target)
    if _sha256(target) != expected:
        raise ChecksumMismatchError("artifact sha256 mismatch")
    return target


def _extract_artifact(archive: Path, destination: Path) -> None:
    """Extract a zip/tar artifact into destination.

    Archives that wrap everything in a single top-level directory are
    unwrapped so destination directly holds the application contents.
    """
    with tempfile.TemporaryDirectory(prefix="proseforge-extract-") as staging_raw:
        staging = Path(staging_raw)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(staging)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as bundle:
                bundle.extractall(staging, filter="data")
        else:
            raise ValueError("unsupported artifact format")
        entries = list(staging.iterdir())
        source = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
        shutil.copytree(source, destination)


def _default_app_dir() -> Path:
    import proseforge

    return Path(proseforge.__file__).resolve().parents[1]


def _swap_app_dir(app_dir: Path, new_dir: Path, rollback_dir: Path) -> None:
    """Atomically replace app_dir with new_dir, keeping the old one aside."""
    if rollback_dir.exists():
        shutil.rmtree(rollback_dir)
    os.rename(app_dir, rollback_dir)
    try:
        os.rename(new_dir, app_dir)
    except OSError:
        # Restore the original layout before propagating the failure.
        os.rename(rollback_dir, app_dir)
        raise


def _restore_app_dir(app_dir: Path, rollback_dir: Path) -> bool:
    """Best-effort restore of the pre-update app dir; True on success."""
    try:
        if app_dir.exists():
            shutil.rmtree(app_dir)
        os.rename(rollback_dir, app_dir)
        return True
    except OSError:
        return False


def _database_url(args_url: str | None, data_dir: Path) -> str:
    database_path = data_dir / "proseforge.sqlite3"
    return args_url or os.getenv("PROSEFORGE_DATABASE_URL") or f"sqlite+aiosqlite:///{database_path.as_posix()}"


def run_update(
    *,
    data_dir: str | None = None,
    backup_dir: str | None = None,
    database_url: str | None = None,
) -> int:
    """Execute the self-update flow and print a single JSON result object."""
    env = dict(os.environ)
    if data_dir:
        env["PROSEFORGE_DATA_DIR"] = data_dir
    paths = resolve_paths(RuntimeProfile.NATIVE, env)
    data = Path(paths.data_dir)
    backups = Path(backup_dir) if backup_dir else Path(paths.backup_dir)
    resolved_url = _database_url(database_url, data)

    from version import get_version

    current = get_version()
    base_url = env.get(UPDATE_BASE_ENV, DEFAULT_UPDATE_BASE)
    stage = "fetch"
    rollback_dir: Path | None = None

    try:
        manifest = fetch_latest(base_url)
        latest = str(manifest["version"])
        if not is_newer(latest, current):
            print(json.dumps({"status": "up_to_date", "version": current, "latest": latest}, sort_keys=True))
            return 0
        artifact = manifest["artifacts"][_platform_key()]
        app_dir = Path(env.get(APP_DIR_ENV, "")) if env.get(APP_DIR_ENV) else _default_app_dir()
        app_dir = app_dir.resolve()
        new_dir = app_dir.parent / f"{app_dir.name}.new"
        rollback_dir = app_dir.parent / f"{app_dir.name}.rollback"

        stage = "download"
        with tempfile.TemporaryDirectory(prefix="proseforge-update-") as staging_raw:
            archive = _download_verified(artifact, Path(staging_raw))
            stage = "extract"
            if new_dir.exists():
                shutil.rmtree(new_dir)
            _extract_artifact(archive, new_dir)

        stage = "stop"
        stop_result = service.stop_service(data)
        if stop_result["status"] == "stop_failed":
            raise RuntimeError("could not stop the running service")

        stage = "swap"
        _swap_app_dir(app_dir, new_dir, rollback_dir)

        stage = "migrate"
        from proseforge.cli.commands.doctor import doctor_report

        def _doctor() -> None:
            if doctor_report(profile=RuntimeProfile.NATIVE, data_dir=data)["status"] != "ok":
                raise RuntimeError("post-update health check failed")

        report_path = run_upgrade(
            data_dir=data,
            backup_dir=backups,
            migrate=alembic_migration_callable(resolved_url),
            doctor=_doctor,
            database_url=resolved_url,
        )

        stage = "start"
        service.start_service(data)
        if rollback_dir.exists():
            shutil.rmtree(rollback_dir, ignore_errors=True)
        print(json.dumps({
            "status": "updated",
            "version_before": current,
            "version_after": latest,
            "report": str(report_path),
            "data_dir": str(data),
        }, sort_keys=True))
        return 0
    except ChecksumMismatchError:
        print(json.dumps({
            "status": "failed",
            "stage": "download",
            "reason": "checksum_mismatch",
            "error_type": "ChecksumMismatchError",
            "data_dir": str(data),
        }, sort_keys=True))
        return 2
    except urllib.error.URLError as exc:
        # Network failures: clean error, no traceback, no URL with credentials.
        print(json.dumps({
            "status": "failed",
            "stage": stage,
            "error_type": type(exc).__name__,
            "data_dir": str(data),
        }, sort_keys=True))
        return 1
    except Exception as exc:
        rolled_back: bool | None = None
        if rollback_dir is not None and rollback_dir.exists():
            rolled_back = _restore_app_dir(app_dir, rollback_dir)
        # Error type names only: messages may embed URLs or connection strings.
        print(json.dumps({
            "status": "failed",
            "stage": stage,
            "error_type": type(exc).__name__,
            "rolled_back": rolled_back,
            "data_dir": str(data),
        }, sort_keys=True))
        return 1
