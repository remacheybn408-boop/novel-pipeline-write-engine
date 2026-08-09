"""``proseforge start|stop|status`` subcommands: native service lifecycle.

Manages a background ``proseforge web`` process through a pidfile in the
native data directory (``<data_dir>/proseforge.pid``). Cross-platform:

- start: Windows detaches via DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP;
  POSIX uses start_new_session=True. stdout/stderr go to logs/service.log.
- stop: Windows tries CTRL_BREAK_EVENT first, then taskkill /T /F; POSIX
  sends SIGTERM, waits up to 10s, then SIGKILL.

The ``*_service`` helpers return result dicts without printing so the
``update`` command can reuse them; the ``run_*`` entry points wrap them
with the single-JSON-object output style used by ``upgrade``.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from proseforge.runtime.paths import resolve_paths
from proseforge.runtime.profile import RuntimeProfile

_PIDFILE_NAME = "proseforge.pid"
_STOP_TIMEOUT_SECONDS = 10.0
_STOP_POLL_INTERVAL = 0.1
_PORT_PROBE_TIMEOUT = 1.0


def _resolve_data_dir(data_dir: str | None) -> Path:
    env = dict(os.environ)
    if data_dir:
        env["PROSEFORGE_DATA_DIR"] = data_dir
    paths = resolve_paths(RuntimeProfile.NATIVE, env)
    return Path(paths.data_dir)


def pidfile_path(data_dir: Path) -> Path:
    return data_dir / _PIDFILE_NAME


def read_pidfile(pidfile: Path) -> int | None:
    """Return the pid stored in the pidfile, or None if missing/invalid."""
    try:
        raw = pidfile.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_alive_windows(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def pid_alive(pid: int) -> bool:
    """Cross-platform liveness probe for a pid."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user.
        return True
    except OSError:
        return False
    return True


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_PORT_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def service_pid(data_dir: Path) -> int | None:
    """Return the live service pid, clearing a stale pidfile; None if down."""
    pidfile = pidfile_path(data_dir)
    pid = read_pidfile(pidfile)
    if pid is None:
        return None
    if pid_alive(pid):
        return pid
    pidfile.unlink(missing_ok=True)
    return None


def start_service(data_dir: Path, *, host: str = "127.0.0.1", port: int = 8000) -> dict[str, object]:
    """Spawn a detached ``proseforge web`` child and record its pid."""
    existing = service_pid(data_dir)
    if existing is not None:
        return {"status": "already_running", "pid": existing}
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "service.log"
    command = [sys.executable, "-m", "proseforge.cli.main", "web", "--host", host, "--port", str(port)]
    with log_path.open("ab") as log:
        if os.name == "nt":
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )
        else:
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
    pidfile_path(data_dir).write_text(str(process.pid), encoding="utf-8")
    return {"status": "started", "pid": process.pid, "log": str(log_path)}


def _terminate_windows(pid: int) -> None:
    """Best-effort graceful stop on Windows, escalating to taskkill /T /F."""
    try:
        os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
    except OSError:
        pass
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(_STOP_POLL_INTERVAL)
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)


def _terminate_posix(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(_STOP_POLL_INTERVAL)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def stop_service(data_dir: Path) -> dict[str, object]:
    """Terminate the service recorded in the pidfile, then clear it."""
    pidfile = pidfile_path(data_dir)
    pid = service_pid(data_dir)
    if pid is None:
        return {"status": "not_running"}
    if os.name == "nt":
        _terminate_windows(pid)
    else:
        _terminate_posix(pid)
    pidfile.unlink(missing_ok=True)
    stopped = not pid_alive(pid)
    return {"status": "stopped" if stopped else "stop_failed", "pid": pid}


def status_service(data_dir: Path, *, host: str = "127.0.0.1", port: int = 8000) -> dict[str, object]:
    """Combine pidfile liveness with a TCP port probe."""
    pid = service_pid(data_dir)
    port_open = _port_open(host, port) if pid is not None else False
    return {
        "status": "running" if (pid is not None and port_open) else "stopped",
        "pid": pid,
        "pid_alive": pid is not None,
        "port_open": port_open,
        "host": host,
        "port": port,
    }


def run_start(*, data_dir: str | None = None, host: str = "127.0.0.1", port: int = 8000) -> int:
    data = _resolve_data_dir(data_dir)
    result = start_service(data, host=host, port=port)
    print(json.dumps(result | {"data_dir": str(data)}, sort_keys=True))
    return 0


def run_stop(*, data_dir: str | None = None) -> int:
    data = _resolve_data_dir(data_dir)
    result = stop_service(data)
    print(json.dumps(result | {"data_dir": str(data)}, sort_keys=True))
    return 0 if result["status"] != "stop_failed" else 1


def run_status(*, data_dir: str | None = None, host: str = "127.0.0.1", port: int = 8000) -> int:
    data = _resolve_data_dir(data_dir)
    result = status_service(data, host=host, port=port)
    print(json.dumps(result | {"data_dir": str(data)}, sort_keys=True))
    return 0 if result["status"] == "running" else 1
