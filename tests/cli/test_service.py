"""Tests for the start/stop/status service commands (all subprocess work mocked)."""

from __future__ import annotations

import json
import os

import pytest

from proseforge.cli.commands import service


@pytest.fixture(autouse=True)
def _clean_env():
    """Isolate PROSEFORGE_* env vars so path resolution stays deterministic."""
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
def fake_processes(monkeypatch):
    """Mock Popen + pid_alive so no real process is ever spawned."""
    alive: set[int] = set()
    spawned: list[dict] = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            self.pid = 4242
            alive.add(self.pid)
            spawned.append({"command": command, "kwargs": kwargs})

    monkeypatch.setattr(service.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(service, "pid_alive", lambda pid: pid in alive)
    return {"alive": alive, "spawned": spawned}


class TestPidfileLifecycle:
    def test_start_spawns_and_writes_pidfile(self, data_dir, fake_processes):
        result = service.start_service(data_dir)
        assert result["status"] == "started"
        assert result["pid"] == 4242
        assert service.read_pidfile(service.pidfile_path(data_dir)) == 4242
        spawned = fake_processes["spawned"][0]
        assert spawned["command"][1:4] == ["-m", "proseforge.cli.main", "web"]
        assert (data_dir / "logs" / "service.log").exists()

    def test_start_twice_reports_already_running(self, data_dir, fake_processes):
        first = service.start_service(data_dir)
        second = service.start_service(data_dir)
        assert first["status"] == "started"
        assert second == {"status": "already_running", "pid": 4242}
        assert len(fake_processes["spawned"]) == 1

    def test_start_replaces_stale_pidfile(self, data_dir, fake_processes):
        service.pidfile_path(data_dir).parent.mkdir(parents=True, exist_ok=True)
        service.pidfile_path(data_dir).write_text("9999", encoding="utf-8")
        result = service.start_service(data_dir)
        assert result["status"] == "started"
        assert service.read_pidfile(service.pidfile_path(data_dir)) == 4242

    def test_stop_without_pidfile(self, data_dir):
        assert service.stop_service(data_dir) == {"status": "not_running"}

    def test_stop_terminates_and_clears_pidfile(self, data_dir, fake_processes, monkeypatch):
        service.start_service(data_dir)
        alive = fake_processes["alive"]

        def kill(data_pid):  # terminate helpers receive the pid, not the dir
            alive.discard(data_pid)

        monkeypatch.setattr(service, "_terminate_windows", kill)
        monkeypatch.setattr(service, "_terminate_posix", kill)
        result = service.stop_service(data_dir)
        assert result == {"status": "stopped", "pid": 4242}
        assert not service.pidfile_path(data_dir).exists()

    def test_stop_clears_stale_pidfile(self, data_dir, fake_processes):
        data_dir.mkdir(parents=True)
        service.pidfile_path(data_dir).write_text("9999", encoding="utf-8")
        result = service.stop_service(data_dir)
        assert result == {"status": "not_running"}
        assert not service.pidfile_path(data_dir).exists()

    def test_read_pidfile_rejects_garbage(self, data_dir):
        data_dir.mkdir(parents=True)
        pidfile = service.pidfile_path(data_dir)
        assert service.read_pidfile(pidfile) is None
        pidfile.write_text("not-a-pid", encoding="utf-8")
        assert service.read_pidfile(pidfile) is None
        pidfile.write_text("-3", encoding="utf-8")
        assert service.read_pidfile(pidfile) is None


class TestStatusStates:
    def test_status_no_pidfile_is_stopped(self, data_dir):
        result = service.status_service(data_dir)
        assert result["status"] == "stopped"
        assert result["pid"] is None
        assert result["pid_alive"] is False
        assert result["port_open"] is False

    def test_status_dead_pid_is_stopped(self, data_dir, fake_processes):
        data_dir.mkdir(parents=True)
        service.pidfile_path(data_dir).write_text("9999", encoding="utf-8")
        result = service.status_service(data_dir)
        assert result["status"] == "stopped"
        assert result["pid"] is None
        # Stale pidfile is cleaned up as a side effect.
        assert not service.pidfile_path(data_dir).exists()

    def test_status_alive_but_port_closed_is_stopped(self, data_dir, fake_processes, monkeypatch):
        service.start_service(data_dir)
        monkeypatch.setattr(service, "_port_open", lambda host, port: False)
        result = service.status_service(data_dir)
        assert result["status"] == "stopped"
        assert result["pid_alive"] is True
        assert result["port_open"] is False

    def test_status_alive_and_port_open_is_running(self, data_dir, fake_processes, monkeypatch):
        service.start_service(data_dir)
        monkeypatch.setattr(service, "_port_open", lambda host, port: True)
        result = service.status_service(data_dir)
        assert result["status"] == "running"
        assert result["pid"] == 4242
        assert result["port_open"] is True


class TestCliWrappers:
    def test_run_start_prints_json(self, data_dir, fake_processes, capsys):
        assert service.run_start(data_dir=str(data_dir)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "started"
        assert payload["pid"] == 4242
        assert payload["data_dir"] == str(data_dir)

    def test_run_stop_exit_codes(self, data_dir, fake_processes, monkeypatch, capsys):
        assert service.run_stop(data_dir=str(data_dir)) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "not_running"

    def test_run_status_exit_codes(self, data_dir, fake_processes, monkeypatch, capsys):
        assert service.run_status(data_dir=str(data_dir)) == 1
        service.start_service(data_dir)
        monkeypatch.setattr(service, "_port_open", lambda host, port: True)
        assert service.run_status(data_dir=str(data_dir)) == 0
        payload = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert payload["status"] == "running"
