"""Offline tests for the bubblewrap sandbox runner.

No bwrap/systemd on the dev machine: every subprocess is faked. What IS
tested for real: command construction, truncation, exit classification,
stderr summarizing, output-file whitelist/magic filtering, timeout and
spawn-failure paths, input-file staging.
"""

from __future__ import annotations

import asyncio
import os
from itertools import pairwise

import pytest

from proseforge.infrastructure.sandbox import runner


class FakeProc:
    def __init__(self, cmd, *, out=b"", err=b"", rc=0, sleep=0.0, hook=None):
        self.cmd = cmd
        self._out, self._err, self.returncode = out, err, rc
        self._sleep = sleep
        self._hook = hook
        self.pid = 99999

    async def communicate(self):
        if self._hook is not None:
            self._hook(self.cmd)
        if self._sleep:
            await asyncio.sleep(self._sleep)
        return self._out, self._err

    async def wait(self):
        return self.returncode

    def kill(self):
        return None


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Patch asyncio.create_subprocess_exec; returns the recorded commands."""
    commands: list[list[str]] = []
    factory_state = {"kwargs": {}}

    async def factory(*cmd, **kwargs):
        commands.append(list(cmd))
        return FakeProc(list(cmd), **factory_state["kwargs"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", factory)
    monkeypatch.setattr(runner, "_RESOURCE_CACHE", {"python": "3.12.0"})  # skip the probe subprocess
    return commands, factory_state


# ---------- command construction ----------


def test_command_layers_and_order():
    cmd = runner.build_bwrap_command(work_dir="/tmp/sbx/work", input_dir="/tmp/sbx/input", venv_path="/opt/proseforge/sandbox-venv")
    assert cmd[0] == "systemd-run" and "--collect" in cmd
    assert "MemoryMax=1G" in cmd and "CPUQuota=200%" in cmd and "TasksMax=64" in cmd and "MemorySwapMax=0" in cmd
    bwrap_at = cmd.index("bwrap")
    for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv"):
        assert flag in cmd[bwrap_at:], flag
    for bind in ("/usr", "/lib", "/lib64"):
        assert ("--ro-bind", bind, bind) in zip(cmd, cmd[1:], cmd[2:])
    assert ("/opt/proseforge/sandbox-venv", "/sandbox-venv") in pairwise(cmd)
    # rw work bind precedes the ro input bind mounted over it
    work_at = cmd.index("/tmp/sbx/work")
    input_at = cmd.index("/tmp/sbx/input")
    assert work_at < input_at and cmd[input_at - 1] == "--ro-bind"
    assert cmd[-2:] == ["/sandbox-venv/bin/python", "/work/task.py"]
    assert "65534" in cmd
    # no input dir -> no ro-bind for /work/input
    cmd_no_input = runner.build_bwrap_command(work_dir="/tmp/sbx/work", input_dir=None, venv_path="/v")
    assert "/work/input" not in cmd_no_input


# ---------- pure helpers ----------


def test_truncate_middle():
    text = "a" * 100 + "b" * 10000 + "c" * 100
    out = runner.truncate_middle(text, 1000)
    assert out.startswith("a" * 50) and out.endswith("c" * 50) and "[truncated:" in out
    assert runner.truncate_middle("short", 100) == "short"


def test_classify_exit():
    assert runner.classify_exit(0, "") == "ok"
    assert runner.classify_exit(137, "") == "oom"
    assert runner.classify_exit(1, "Traceback...\nMemoryError") == "oom"
    assert runner.classify_exit(1, "out of memory killer") == "oom"
    assert runner.classify_exit(2, "SyntaxError") == "crashed"


def test_summarize_stderr():
    assert runner.summarize_stderr("") == ""
    stderr = "line1\nline2\nline3\nline4"
    assert runner.summarize_stderr(stderr) == "line2\nline3\nline4"
    assert len(runner.summarize_stderr("x" * 5000)) <= 500


def test_collect_output_files_whitelist(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "ok.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    (out / "fake.png").write_bytes(b"not a png")
    (out / "evil.exe").write_bytes(b"MZ" + b"0" * 100)
    (out / "data.csv").write_text("a,b\n1,2\n")
    (out / "big.txt").write_bytes(b"x" * 2048)
    (out / "subdir").mkdir()
    files = runner.collect_output_files(str(tmp_path), max_file_bytes=1024)
    names = [item["name"] for item in files]
    assert names == ["data.csv", "ok.png"]  # big.txt over cap, fake.png bad magic, exe not whitelisted
    assert files[0]["data"].startswith(b"a,b")
    assert files[1]["mime"] == "image/png"


def test_collect_output_files_count_cap(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for index in range(8):
        (out / f"f{index}.txt").write_text("x")
    assert len(runner.collect_output_files(str(tmp_path), max_files=5)) == 5


# ---------- run_python end-to-end (faked subprocess) ----------


@pytest.mark.asyncio
async def test_run_ok_collects_files_and_stdout(fake_subprocess):
    commands, state = fake_subprocess

    def hook(cmd):
        # While "running": task.py exists and we can drop an artifact.
        work_dir = cmd[cmd.index("--bind") + 1]
        assert os.path.isfile(os.path.join(work_dir, "task.py"))
        with open(os.path.join(work_dir, "out", "result.csv"), "wb") as handle:
            handle.write(b"a,b\n1,2\n")

    state["kwargs"] = {"out": b"hello stdout", "err": b"", "rc": 0, "hook": hook}
    result = await runner.run_python("print('hi')", timeout_seconds=10)
    assert result["status"] == "ok" and result["exit_code"] == 0
    assert result["stdout"] == "hello stdout"
    assert [f["name"] for f in result["files"]] == ["result.csv"]
    assert result["files"][0]["data"] == b"a,b\n1,2\n"
    assert result["resource"]["python"] == "3.12.0"
    assert result["duration_ms"] >= 0
    assert commands and commands[0][0] == "systemd-run"


@pytest.mark.asyncio
async def test_run_timeout_kills_and_classifies(fake_subprocess):
    _, state = fake_subprocess
    state["kwargs"] = {"sleep": 5.0}
    result = await runner.run_python("while True: pass", timeout_seconds=1)
    assert result["status"] == "timeout" and result["exit_code"] is None


@pytest.mark.asyncio
async def test_run_oom_and_crashed(fake_subprocess):
    _, state = fake_subprocess
    state["kwargs"] = {"rc": 137, "err": b"Killed"}
    assert (await runner.run_python("x = [0]*10**10", timeout_seconds=10))["status"] == "oom"
    state["kwargs"] = {"rc": 1, "err": b"Traceback (most recent call last):\nMemoryError"}
    assert (await runner.run_python("x = [0]*10**10", timeout_seconds=10))["status"] == "oom"
    state["kwargs"] = {"rc": 2, "err": b"SyntaxError: invalid syntax"}
    crashed = await runner.run_python("def (", timeout_seconds=10)
    assert crashed["status"] == "crashed" and "SyntaxError" in crashed["stderr_summary"]


@pytest.mark.asyncio
async def test_spawn_failure(monkeypatch):
    monkeypatch.setattr(runner, "_RESOURCE_CACHE", {"python": "3.12.0"})

    async def broken(*cmd, **kwargs):
        raise FileNotFoundError("bwrap: command not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", broken)
    result = await runner.run_python("pass", timeout_seconds=10)
    assert result["status"] == "spawn_failed" and "bwrap" in result["stderr_summary"]


@pytest.mark.asyncio
async def test_input_files_staged_read_only_bind(fake_subprocess):
    _, state = fake_subprocess
    seen = {}

    def hook(cmd):
        input_dir = cmd[cmd.index("/work/input") - 1]
        seen["input_dir"] = input_dir
        seen["files"] = sorted(os.listdir(input_dir))

    state["kwargs"] = {"rc": 0, "hook": hook}
    result = await runner.run_python("pass", timeout_seconds=10, input_files=[("data.csv", b"a,b\n"), ("note.txt", b"hi")])
    assert result["status"] == "ok"
    assert seen["files"] == ["data.csv", "note.txt"]


# ---------- registration / contract ----------


def test_registered_and_contracted():
    from proseforge.application.conversations.tool_contract import (
        CODE_RUNNER_SKILL_KEY,
        build_tool_contract,
    )
    from proseforge.application.tools import TOOL_REGISTRY

    tool = TOOL_REGISTRY.get("run_code")
    assert tool is not None and tool.toggle_key == CODE_RUNNER_SKILL_KEY == "builtin-code-runner"
    assert tool.timeout_s >= 120
    contract = build_tool_contract([tool], 4)
    assert "run_code" in contract and "无网络" in contract and "out/" in contract
