"""Tests for the ``update`` command (network, subprocess and migrations mocked)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import urllib.error
import zipfile
from pathlib import Path

import pytest

from proseforge.cli.commands import service, update


@pytest.fixture(autouse=True)
def _clean_env():
    """Isolate PROSEFORGE_* env vars so path/app-dir resolution is deterministic."""
    snapshot = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("PROSEFORGE_"):
            del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    """Fake native install root; pointed at via PROSEFORGE_APP_DIR."""
    app = tmp_path / "ProseForgeApp"
    app.mkdir()
    (app / "app.bin").write_text("old-release", encoding="utf-8")
    monkeypatch.setenv(update.APP_DIR_ENV, str(app))
    return app


def _make_zip(payload: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in payload.items():
            bundle.writestr(name, content)
    return buffer.getvalue()


def _manifest(latest: str, artifact_bytes: bytes | None = None, url: str = "https://example.invalid/app.zip") -> dict:
    artifact = {"url": url, "sha256": hashlib.sha256(artifact_bytes or b"").hexdigest()}
    return {"version": latest, "artifacts": {"windows": artifact, "linux": artifact}}


@pytest.fixture
def mock_feed(monkeypatch):
    """Replace fetch_latest; tests set ``mock_feed.manifest`` or make it raise."""
    state: dict = {"manifest": _manifest("9.9.9"), "error": None}

    def fake_fetch(base_url: str) -> dict:
        if state["error"] is not None:
            raise state["error"]
        return state["manifest"]

    monkeypatch.setattr(update, "fetch_latest", fake_fetch)
    return state


@pytest.fixture
def mock_service(monkeypatch):
    calls: list[str] = []

    def fake_stop(data: Path) -> dict:
        calls.append("stop")
        return {"status": "stopped", "pid": 1}

    def fake_start(data: Path, **kwargs) -> dict:
        calls.append("start")
        return {"status": "started", "pid": 2}

    monkeypatch.setattr(service, "stop_service", fake_stop)
    monkeypatch.setattr(service, "start_service", fake_start)
    return calls


@pytest.fixture
def mock_upgrade(monkeypatch):
    monkeypatch.setattr(update, "alembic_migration_callable", lambda url: (lambda: None))

    def fake_run_upgrade(**kwargs) -> Path:
        return Path(kwargs["backup_dir"]) / "upgrade-report.json"

    monkeypatch.setattr(update, "run_upgrade", fake_run_upgrade)


class TestVersionCompare:
    @pytest.mark.parametrize(
        ("latest", "current", "expected"),
        [
            ("1.5.1", "1.5.0", True),
            ("2.0.0", "1.5.0", True),
            ("1.5.0", "1.5.0", False),
            ("1.5.0", "1.5.1", False),
            ("1.5", "1.5.0", False),
            ("v1.6.0", "1.5.9", True),
            ("1.5.0b1", "1.5.0", False),
        ],
    )
    def test_is_newer(self, latest, current, expected):
        assert update.is_newer(latest, current) is expected

    def test_parse_version_cuts_suffixes(self):
        assert update._parse_version("v1.2.3") == (1, 2, 3)
        assert update._parse_version("1.2rc1") == (1, 2)
        assert update._parse_version("") == (0,)


class TestFetchLatest:
    def test_fetch_latest_parses_json(self, monkeypatch):
        payload = json.dumps(_manifest("9.9.9")).encode("utf-8")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return payload

        monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout: FakeResponse())
        assert update.fetch_latest("https://example.invalid/releases")["version"] == "9.9.9"


class TestRunUpdate:
    def test_up_to_date_exits_zero(self, data_dir, mock_feed, capsys):
        from version import get_version

        mock_feed["manifest"] = _manifest(get_version())
        assert update.run_update(data_dir=str(data_dir)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "up_to_date"
        assert payload["version"] == payload["latest"]

    def test_older_feed_version_exits_zero(self, data_dir, mock_feed, capsys):
        mock_feed["manifest"] = _manifest("0.0.1")
        assert update.run_update(data_dir=str(data_dir)) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "up_to_date"

    def test_checksum_mismatch_rejects_with_exit_2(self, data_dir, app_dir, mock_feed, monkeypatch, capsys):
        mock_feed["manifest"] = _manifest("9.9.9", artifact_bytes=b"expected-bytes")
        # Downloader writes bytes that do not match the manifest sha256.
        monkeypatch.setattr(update, "_download", lambda url, dest: dest.write_bytes(b"tampered-bytes"))
        assert update.run_update(data_dir=str(data_dir)) == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "failed"
        assert payload["reason"] == "checksum_mismatch"
        # Nothing was touched: no .new/.rollback dirs, app dir intact.
        assert (app_dir / "app.bin").read_text(encoding="utf-8") == "old-release"
        assert not list(app_dir.parent.glob("*.rollback"))

    def test_network_failure_is_clean(self, data_dir, mock_feed, capsys):
        mock_feed["error"] = urllib.error.URLError("no route to host")
        assert update.run_update(data_dir=str(data_dir)) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "failed"
        assert payload["stage"] == "fetch"
        assert payload["error_type"] == "URLError"

    def test_happy_path_swaps_and_reports(
        self, data_dir, app_dir, mock_feed, mock_service, mock_upgrade, monkeypatch, capsys
    ):
        artifact = _make_zip({"ProseForgeApp/app.bin": "new-release"})
        mock_feed["manifest"] = _manifest("9.9.9", artifact_bytes=artifact)
        monkeypatch.setattr(update, "_download", lambda url, dest: dest.write_bytes(artifact))
        assert update.run_update(data_dir=str(data_dir)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "updated"
        assert payload["version_after"] == "9.9.9"
        assert mock_service == ["stop", "start"]
        assert (app_dir / "app.bin").read_text(encoding="utf-8") == "new-release"
        # Rollback copy is discarded after a successful update.
        assert not (app_dir.parent / f"{app_dir.name}.rollback").exists()
        assert not (app_dir.parent / f"{app_dir.name}.new").exists()

    def test_migration_failure_rolls_back(
        self, data_dir, app_dir, mock_feed, mock_service, monkeypatch, capsys
    ):
        artifact = _make_zip({"app.bin": "new-release"})
        mock_feed["manifest"] = _manifest("9.9.9", artifact_bytes=artifact)
        monkeypatch.setattr(update, "_download", lambda url, dest: dest.write_bytes(artifact))
        monkeypatch.setattr(update, "alembic_migration_callable", lambda url: (lambda: None))

        def failing_upgrade(**kwargs):
            raise RuntimeError("migration exploded; dsn=sqlite://secret")

        monkeypatch.setattr(update, "run_upgrade", failing_upgrade)
        assert update.run_update(data_dir=str(data_dir)) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "failed"
        assert payload["stage"] == "migrate"
        assert payload["error_type"] == "RuntimeError"
        assert payload["rolled_back"] is True
        # Only the type name leaks, never the exception message.
        assert "secret" not in json.dumps(payload)
        # App dir content is restored from .rollback.
        assert (app_dir / "app.bin").read_text(encoding="utf-8") == "old-release"
        # Service was stopped but never restarted.
        assert mock_service == ["stop"]


class TestCliRegistration:
    def test_main_dispatches_update(self, monkeypatch):
        captured = {}

        def fake_run_update(**kwargs):
            captured.update(kwargs)
            return 0

        monkeypatch.setattr("proseforge.cli.commands.update.run_update", fake_run_update)
        from proseforge.cli.main import main

        assert main(["update", "--data-dir", "/tmp/x"]) == 0
        assert captured == {"data_dir": "/tmp/x", "backup_dir": None, "database_url": None}

    @pytest.mark.parametrize("command", ["start", "stop", "status"])
    def test_main_dispatches_service_commands(self, monkeypatch, command):
        captured = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(f"proseforge.cli.commands.service.run_{command}", fake_run)
        from proseforge.cli.main import main

        assert main([command]) == 0
        assert "data_dir" in captured
