from __future__ import annotations

import base64
import os
from typing import ClassVar

import pytest

from proseforge.cli.commands import web as web_module
from proseforge.cli.commands.web import run_web
from proseforge.cli.main import main


@pytest.fixture(autouse=True)
def _clean_env():
    """隔离 PROSEFORGE_* 环境变量：run_web 直接写 os.environ，跑完必须还原。"""
    snapshot = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("PROSEFORGE_"):
            del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(snapshot)


class FakeServer:
    instances: ClassVar[list] = []

    def __init__(self, config):
        self.config = config
        self.ran = False
        FakeServer.instances.append(self)

    def run(self):
        self.ran = True


@pytest.fixture
def fake_server(monkeypatch):
    FakeServer.instances = []
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    return FakeServer


@pytest.fixture
def no_frontend(monkeypatch):
    """固定 locate_frontend_dir，避免真实仓库 apps/web/dist 干扰断言。"""
    monkeypatch.setattr(web_module, "locate_frontend_dir", lambda env: None)


def test_run_web_assembles_native_env(tmp_path, fake_server, no_frontend):
    data_dir = tmp_path / "data"
    assert run_web(host="127.0.0.1", port=8901, data_dir=str(data_dir), frontend_dir=None) == 0
    assert os.environ["PROSEFORGE_RUNTIME_PROFILE"] == "native"
    assert os.environ["PROSEFORGE_DATA_DIR"] == str(data_dir)
    expected_db = f"sqlite+aiosqlite:///{(data_dir / 'proseforge.sqlite3').as_posix()}"
    assert os.environ["PROSEFORGE_DATABASE_URL"] == expected_db
    assert os.environ["PROSEFORGE_BLOB_ROOT"] == str(data_dir / "blobs")
    assert os.environ["PROSEFORGE_BACKUP_ROOT"] == str(data_dir / "backups")
    assert os.environ["PROSEFORGE_PUBLIC_URL"] == "http://127.0.0.1:8901"
    assert "PROSEFORGE_SERVE_WEB" not in os.environ


def test_run_web_preserves_user_env(tmp_path, fake_server, no_frontend):
    os.environ["PROSEFORGE_DATABASE_URL"] = "sqlite+aiosqlite:////custom/db.sqlite3"
    os.environ["PROSEFORGE_MASTER_KEY"] = "user-provided-master-key"
    os.environ["PROSEFORGE_JWT_SECRET"] = "user-provided-jwt-secret"
    data_dir = tmp_path / "data"
    assert run_web(host="127.0.0.1", port=8901, data_dir=str(data_dir), frontend_dir=None) == 0
    assert os.environ["PROSEFORGE_DATABASE_URL"] == "sqlite+aiosqlite:////custom/db.sqlite3"
    assert os.environ["PROSEFORGE_MASTER_KEY"] == "user-provided-master-key"
    assert os.environ["PROSEFORGE_JWT_SECRET"] == "user-provided-jwt-secret"
    assert not (data_dir / "master.key").exists()
    assert not (data_dir / "jwt.key").exists()


def test_run_web_preserves_user_public_url(tmp_path, fake_server, no_frontend):
    os.environ["PROSEFORGE_PUBLIC_URL"] = "https://novels.example.com"
    assert run_web(host="127.0.0.1", port=8901, data_dir=str(tmp_path / "data"), frontend_dir=None) == 0
    assert os.environ["PROSEFORGE_PUBLIC_URL"] == "https://novels.example.com"


def test_run_web_generates_then_reuses_keys(tmp_path, fake_server, no_frontend, capsys):
    data_dir = tmp_path / "data"
    assert run_web(host="127.0.0.1", port=8901, data_dir=str(data_dir), frontend_dir=None) == 0
    master = os.environ["PROSEFORGE_MASTER_KEY"]
    jwt = os.environ["PROSEFORGE_JWT_SECRET"]
    assert len(base64.b64decode(master)) == 32
    assert len(jwt.encode("utf-8")) >= 32
    assert (data_dir / "master.key").read_text(encoding="utf-8").strip() == master
    assert (data_dir / "jwt.key").read_text(encoding="utf-8").strip() == jwt
    out = capsys.readouterr().out
    assert master not in out
    assert jwt not in out
    # 模拟新进程：清掉 env，二次运行必须从 key 文件复用同一值
    del os.environ["PROSEFORGE_MASTER_KEY"]
    del os.environ["PROSEFORGE_JWT_SECRET"]
    assert run_web(host="127.0.0.1", port=8901, data_dir=str(data_dir), frontend_dir=None) == 0
    assert os.environ["PROSEFORGE_MASTER_KEY"] == master
    assert os.environ["PROSEFORGE_JWT_SECRET"] == jwt
    out = capsys.readouterr().out
    assert master not in out
    assert jwt not in out


def test_run_web_enables_serve_web_when_frontend_found(tmp_path, fake_server, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    monkeypatch.setattr(web_module, "locate_frontend_dir", lambda env: frontend)
    assert run_web(host="127.0.0.1", port=8901, data_dir=str(tmp_path / "data"), frontend_dir=None) == 0
    assert os.environ["PROSEFORGE_FRONTEND_DIR"] == str(frontend)
    assert os.environ["PROSEFORGE_SERVE_WEB"] == "true"


def test_run_web_frontend_dir_flag_beats_locate(tmp_path, fake_server, no_frontend):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    assert run_web(host="127.0.0.1", port=8901, data_dir=str(tmp_path / "data"), frontend_dir=str(frontend)) == 0
    assert os.environ["PROSEFORGE_FRONTEND_DIR"] == str(frontend)
    assert os.environ["PROSEFORGE_SERVE_WEB"] == "true"


def test_run_web_warns_when_frontend_missing(tmp_path, fake_server, no_frontend, capsys):
    assert run_web(host="127.0.0.1", port=8901, data_dir=str(tmp_path / "data"), frontend_dir=None) == 0
    out = capsys.readouterr().out
    assert "frontend" in out.lower()
    assert "PROSEFORGE_FRONTEND_DIR" not in os.environ
    assert "PROSEFORGE_SERVE_WEB" not in os.environ


def test_run_web_configures_uvicorn(tmp_path, fake_server, no_frontend, capsys):
    assert run_web(host="0.0.0.0", port=9000, data_dir=str(tmp_path / "data"), frontend_dir=None) == 0
    server = fake_server.instances[0]
    assert server.config.app == "proseforge.api.main:app"
    assert server.config.host == "0.0.0.0"
    assert server.config.port == 9000
    assert server.ran
    assert f"http://{server.config.host}:{server.config.port}" in capsys.readouterr().out


def test_cli_web_forwards_arguments(monkeypatch):
    captured = {}

    def fake_run_web(*, host, port, data_dir, frontend_dir):
        captured.update(host=host, port=port, data_dir=data_dir, frontend_dir=frontend_dir)
        return 0

    monkeypatch.setattr("proseforge.cli.commands.web.run_web", fake_run_web)
    assert main(["web", "--port", "8901"]) == 0
    assert captured == {"host": "127.0.0.1", "port": 8901, "data_dir": None, "frontend_dir": None}
