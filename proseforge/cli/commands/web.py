"""``proseforge web`` 子命令：native profile 一键启动（V15-006）。

组装 native 运行环境（数据目录、SQLite DATABASE_URL、密钥持久化、前端
静态目录），然后用 uvicorn 同源托管 API + SPA。所有环境变量只在用户未
显式设置时补默认值；密钥值绝不打印到日志/stdout。
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from proseforge.runtime.paths import resolve_paths
from proseforge.runtime.profile import RuntimeProfile
from proseforge.runtime.web_assets import locate_frontend_dir

_DATABASE_NAME = "proseforge.sqlite3"


def _setdefault_env(name: str, value: str) -> None:
    if not os.environ.get(name):
        os.environ[name] = value


def _ensure_secret(env_name: str, key_path: Path) -> None:
    """从 data_dir 下的 key 文件读取或生成密钥并写入 env；绝不打印密钥值。"""
    if os.environ.get(env_name):
        return
    value = key_path.read_text(encoding="utf-8").strip() if key_path.is_file() else ""
    if not value:
        value = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        key_path.write_text(value, encoding="utf-8")
        try:
            key_path.chmod(0o600)
        except OSError:
            pass  # Windows 上 chmod 可能失败，忽略
    os.environ[env_name] = value


def run_web(*, host: str, port: int, data_dir: str | None, frontend_dir: str | None) -> int:
    """以 native profile 启动 ProseForge（API + 可选 SPA），返回进程退出码。"""
    _setdefault_env("PROSEFORGE_RUNTIME_PROFILE", RuntimeProfile.NATIVE.value)
    if data_dir:
        os.environ["PROSEFORGE_DATA_DIR"] = data_dir
    paths = resolve_paths(RuntimeProfile.NATIVE, os.environ)
    _setdefault_env("PROSEFORGE_DATA_DIR", str(paths.data_dir))
    data = Path(os.environ["PROSEFORGE_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    _setdefault_env("PROSEFORGE_DATABASE_URL", f"sqlite+aiosqlite:///{(data / _DATABASE_NAME).as_posix()}")
    _setdefault_env("PROSEFORGE_BLOB_ROOT", str(paths.blob_dir))
    _setdefault_env("PROSEFORGE_BACKUP_ROOT", str(paths.backup_dir))
    # SPA 与 API 同源：浏览器 Origin 即 http://host:port，必须与服务端
    # require_same_origin 校验的 public_url 一致，否则同源 POST 全部 403。
    _setdefault_env("PROSEFORGE_PUBLIC_URL", f"http://{host}:{port}")

    resolved_frontend = Path(frontend_dir) if frontend_dir else locate_frontend_dir(os.environ)
    if resolved_frontend is not None and resolved_frontend.is_dir():
        os.environ["PROSEFORGE_FRONTEND_DIR"] = str(resolved_frontend)
        _setdefault_env("PROSEFORGE_SERVE_WEB", "true")
    else:
        print("ProseForge web frontend not found; serving API only")

    _ensure_secret("PROSEFORGE_MASTER_KEY", data / "master.key")
    _ensure_secret("PROSEFORGE_JWT_SECRET", data / "jwt.key")

    import uvicorn

    config = uvicorn.Config("proseforge.api.main:app", host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    print(f"ProseForge native listening on http://{host}:{port}")
    server.run()
    return 0
