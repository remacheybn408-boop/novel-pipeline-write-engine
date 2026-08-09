"""Bubblewrap sandbox runner for the run_code tool.

Executes untrusted Python inside ``systemd-run`` (cgroup limits) wrapping
``bwrap`` (namespace isolation: unshare-all, no network, read-only host,
nobody uid). Runs INLINE in the chat generation task — the generation is
already a celery task (queue semantics), and ``--die-with-parent`` ties the
sandbox to the worker process lifetime.

Host layout per run: a private temp dir with ``work/`` (bound rw as /work so
output files can be collected afterwards) and optionally ``input/`` (bound
read-only as /work/input). Everything is removed in a finally.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import tempfile
import time

logger = logging.getLogger(__name__)

DEFAULT_VENV_PATH = "/opt/proseforge/sandbox-venv"
MAX_OUTPUT_CHARS = 64 * 1024
MAX_STDERR_SUMMARY_CHARS = 500
MAX_OUTPUT_FILES = 5
MAX_OUTPUT_FILE_BYTES = 10 * 1024 * 1024
MAX_INPUT_FILE_BYTES = 10 * 1024 * 1024
MAX_INPUT_FILES = 5
RESOURCE_PROBE_TIMEOUT = 10.0


def _write_bytes(path: str, data: bytes) -> None:
    """Sync file write for use with asyncio.to_thread (keeps blocking I/O off the loop)."""
    with open(path, "wb") as handle:
        handle.write(data)


def _write_text(path: str, text: str) -> None:
    """Sync file write for use with asyncio.to_thread (keeps blocking I/O off the loop)."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)

# Output/input whitelist: extension -> (mime, magic check kind)
_FILE_TYPES: dict[str, tuple[str, str]] = {
    ".png": ("image/png", "png"),
    ".jpg": ("image/jpeg", "jpg"),
    ".jpeg": ("image/jpeg", "jpg"),
    ".svg": ("image/svg+xml", "svg"),
    ".csv": ("text/csv", "text"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "zip"),
    ".txt": ("text/plain", "text"),
    ".md": ("text/markdown", "text"),
    ".json": ("application/json", "text"),
}


def build_bwrap_command(*, work_dir: str, input_dir: str | None, venv_path: str) -> list[str]:
    """The full systemd-run + bwrap command (isolation layers documented inline)."""
    command = [
        # Layer 1: cgroup resource caps (1G RAM, no swap, 2 CPUs, 64 tasks).
        "systemd-run", "--scope", "--quiet", "--collect",
        "-p", "MemoryMax=1G", "-p", "CPUQuota=200%", "-p", "TasksMax=64", "-p", "MemorySwapMax=0", "--",
        # Layer 2: namespace isolation — no network, no host IPC, die with parent.
        "bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
        "--setenv", "PATH", "/sandbox-venv/bin:/usr/bin:/bin",
        "--setenv", "MPLBACKEND", "Agg",
        "--setenv", "MPLCONFIGDIR", "/tmp/mpl",
        "--setenv", "OPENBLAS_NUM_THREADS", "1",
        "--setenv", "HOME", "/work",
        # Layer 3: read-only host view; only the venv and work dir are reachable.
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", venv_path, "/sandbox-venv",
        "--tmpfs", "/tmp",
        "--bind", work_dir, "/work",
    ]
    if input_dir is not None:
        # Conversation attachments land here read-only (bind order matters:
        # this mounts over the rw /work).
        command += ["--ro-bind", input_dir, "/work/input"]
    # Layer 4: drop to nobody before exec.
    command += ["--chdir", "/work", "--uid", "65534", "--gid", "65534", "/sandbox-venv/bin/python", "/work/task.py"]
    return command


def truncate_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    half = max_chars // 2
    return f"{text[:half]}\n[truncated: {omitted} chars]\n{text[-(max_chars - half):]}"


def classify_exit(exit_code: int | None, stderr: str) -> str:
    """ok / crashed / oom from the exit code and stderr heuristics."""
    if exit_code == 0:
        return "ok"
    if exit_code == 137 or "MemoryError" in stderr or "out of memory" in stderr.lower():
        return "oom"  # 137 = SIGKILL, the cgroup OOM killer's signature
    return "crashed"


def summarize_stderr(stderr: str, max_chars: int = MAX_STDERR_SUMMARY_CHARS) -> str:
    """User-facing stderr summary: the last meaningful lines (the exception
    line), never the full traceback."""
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-3:])[:max_chars]


def _magic_ok(kind: str, data: bytes) -> bool:
    if kind == "png":
        return data.startswith(b"\x89PNG")
    if kind == "jpg":
        return data.startswith(b"\xff\xd8\xff")
    if kind == "zip":
        return data.startswith(b"PK\x03\x04")
    if kind == "svg":
        head = data[:512].lstrip()
        return head.startswith(b"<") and (b"<svg" in head or b"<?xml" in head)
    try:  # text kinds must decode cleanly
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def collect_output_files(work_dir: str, *, max_files: int = MAX_OUTPUT_FILES, max_file_bytes: int = MAX_OUTPUT_FILE_BYTES) -> list[dict]:
    """Collect whitelisted artifacts from /work/out (magic-checked)."""
    out_dir = os.path.join(work_dir, "out")
    files: list[dict] = []
    if not os.path.isdir(out_dir):
        return files
    for name in sorted(os.listdir(out_dir)):
        if len(files) >= max_files:
            break
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path):
            continue
        file_type = _FILE_TYPES.get(_extension(name))
        if file_type is None:
            continue
        size = os.path.getsize(path)
        if size == 0 or size > max_file_bytes:
            continue
        with open(path, "rb") as handle:
            data = handle.read()
        mime, magic = file_type
        if not _magic_ok(magic, data):
            continue
        files.append({"name": name, "path": f"/work/out/{name}", "size": size, "mime": mime, "data": data})
    return files


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Timeout kill: the whole process group (systemd scope + bwrap children)."""
    killpg = getattr(os, "killpg", None)  # POSIX-only; fall back to the direct child
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        if killpg is not None:
            killpg(proc.pid, sigkill)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


_RESOURCE_CACHE: dict | None = None


async def probe_resource(venv_path: str) -> dict:
    """Best-effort python/package versions for tool_call_log (cached, host-side)."""
    global _RESOURCE_CACHE
    if _RESOURCE_CACHE is not None:
        return _RESOURCE_CACHE
    resource = {"python": "unknown", "venv": venv_path}
    try:
        proc = await asyncio.create_subprocess_exec(
            os.path.join(venv_path, "bin", "python"),
            "-c",
            "import sys, json; print(json.dumps({'python': sys.version.split()[0], "
            "'pkgs': {m: __import__(m).__version__ for m in ('pandas', 'numpy', 'matplotlib', 'openpyxl')}}))",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=RESOURCE_PROBE_TIMEOUT)
        if proc.returncode == 0:
            resource.update(json.loads(out.decode("utf-8", errors="replace")))
    except Exception:  # noqa: S110 -- probe failure must never block a run; silent by design
        pass
    _RESOURCE_CACHE = resource
    return resource


async def run_python(
    code: str,
    *,
    timeout_seconds: int = 60,
    max_timeout_seconds: int = 120,
    venv_path: str = DEFAULT_VENV_PATH,
    input_files: list[tuple[str, bytes]] = (),
    max_output_chars: int = MAX_OUTPUT_CHARS,
    max_files: int = MAX_OUTPUT_FILES,
    max_file_bytes: int = MAX_OUTPUT_FILE_BYTES,
) -> dict:
    """Run untrusted code in the sandbox; return a structured, honest result."""
    timeout = min(max(1, int(timeout_seconds)), max_timeout_seconds)
    started = time.monotonic()
    parent = tempfile.mkdtemp(prefix="proseforge-sbx-")
    status = "ok"
    exit_code: int | None = None
    stdout_text = ""
    stderr_text = ""
    files: list[dict] = []
    try:
        work_dir = os.path.join(parent, "work")
        out_dir = os.path.join(work_dir, "out")
        os.makedirs(out_dir, exist_ok=True)
        # The sandboxed process runs as uid 65534 (nobody) and must be able
        # to write into /work; mkdtemp defaults to 0700 root-only.
        os.chmod(parent, 0o755)
        os.chmod(work_dir, 0o777)
        os.chmod(out_dir, 0o777)
        input_dir = None
        if input_files:
            input_dir = os.path.join(parent, "input")
            os.makedirs(input_dir)
            for name, data in input_files[:MAX_INPUT_FILES]:
                # Blocking file I/O offloaded to a thread: this coroutine runs
                # on the app event loop (ASYNC230).
                await asyncio.to_thread(_write_bytes, os.path.join(input_dir, name), data)
        await asyncio.to_thread(_write_text, os.path.join(work_dir, "task.py"), code)
        command = build_bwrap_command(work_dir=work_dir, input_dir=input_dir, venv_path=venv_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group for the timeout kill
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return {
                "status": "spawn_failed",
                "stdout": "",
                "stderr_summary": str(exc),
                "stderr_full": str(exc),
                "exit_code": None,
                "duration_ms": (time.monotonic() - started) * 1000,
                "files": [],
                "resource": await probe_resource(venv_path),
            }
        try:
            out_bytes, err_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            exit_code = proc.returncode
        except TimeoutError:
            status = "timeout"
            _kill_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                pass
            out_bytes, err_bytes = b"", b""
        stdout_text = truncate_middle(out_bytes.decode("utf-8", errors="replace"), max_output_chars)
        stderr_text = truncate_middle(err_bytes.decode("utf-8", errors="replace"), max_output_chars)
        if status != "timeout":
            status = classify_exit(exit_code, stderr_text)
        if status in {"ok", "crashed"}:
            files = collect_output_files(work_dir, max_files=max_files, max_file_bytes=max_file_bytes)
    finally:
        shutil.rmtree(parent, ignore_errors=True)
    return {
        "status": status,
        "stdout": stdout_text,
        "stderr_summary": summarize_stderr(stderr_text),
        "stderr_full": stderr_text,
        "exit_code": exit_code,
        "duration_ms": (time.monotonic() - started) * 1000,
        "files": files,
        "resource": await probe_resource(venv_path),
    }


def attachment_digest(data: bytes) -> str:
    """sha256 helper kept next to the runner (attachment rows need it)."""
    return hashlib.sha256(data).hexdigest()
