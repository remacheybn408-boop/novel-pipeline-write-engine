#!/usr/bin/env bash
# 构建 PyInstaller onedir 原生包（V15-008）。
# - Linux 宿主：直接调用 packaging.native_bundle（要求 Python 3.12）。
# - Windows/macOS 宿主 + --target linux：在 podman python:3.12 容器内构建。
set -euo pipefail
target="linux"
format="tar.gz"
skip_sign=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) target="$2"; shift 2;;
    --format) format="$2"; shift 2;;
    --skip-sign) skip_sign=1; shift;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$root/artifacts/native/$target"
mkdir -p "$out"
host="$(uname -s)"

if [[ "$host" == "Linux" || ( "$host" == "Darwin" && "$target" == "macos" ) ]]; then
  py_bin="python"
  command -v python >/dev/null 2>&1 || py_bin="python3"
  PYTHONPATH="$root" "$py_bin" -m packaging.native_bundle \
    --root "$root" --output "$out" --target "$target" --format "$format"
elif [[ "$target" == "linux" ]]; then
  # podman 需要 Windows 风格源路径；MSYS_NO_PATHCONV=1 防止 Git Bash 改写 /src。
  # 显式清空代理变量：podman 会把宿主/服务端的 http_proxy 注入容器，
  # 若该代理对容器不可达（如指向宿主 127.0.0.1），pip 会全部失败。
  root_mnt="$(cd "$root" && pwd -W 2>/dev/null || echo "$root")"
  MSYS_NO_PATHCONV=1 podman run --rm \
    -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= -e ALL_PROXY= -e all_proxy= \
    -v "$root_mnt:/src" -w /src python:3.12 bash -lc \
    "pip install -q -e '.[api]' pyinstaller && python -m packaging.native_bundle --root /src --output /src/artifacts/native/linux --target linux --format tar.gz"
else
  echo "unsupported combination: host=$host target=$target" >&2
  echo "run on a matching host, or use --target linux (built via podman python:3.12)" >&2
  exit 2
fi
