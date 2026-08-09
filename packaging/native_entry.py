"""PyInstaller 入口脚本（V15-008）。

原生可执行文件的行为约定：无命令行参数时等价于 ``proseforge web``（以
native profile 启动 uvicorn，同源托管 API + SPA）；带参数时原样透传给
CLI（如 ``proseforge.exe --version`` / ``proseforge.exe doctor --json``）。
"""
from __future__ import annotations

import sys

from proseforge.cli.main import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["web"]))
