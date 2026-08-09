"""Build a native PyInstaller onedir distribution bundle (V15-008).

PyInstaller analyzes a staged copy of the ``proseforge`` package plus the
root ``version.py`` module (never the repo root itself) so the repo's own
top-level ``packaging`` package cannot shadow the third-party ``packaging``
distribution that celery/kombu and other dependencies import.

Build host requirements: Python 3.12 (pinned) and an already-built frontend
(``apps/web/dist``). The target platform must match the build host; Linux
builds on Windows/macOS hosts run inside a podman container via
``scripts/build_native.sh --target linux``.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from packaging.manifest import build_manifest, write_manifest
from version import get_version

_ARCHIVE_SUFFIX = {"zip": ".zip", "tar.gz": ".tar.gz"}
_HOST_TARGETS = {"win32": "windows", "linux": "linux", "darwin": "macos"}
_BUILD_VENV_PREFIX = ".venv-native-"
_EXCLUDED_MODULES = ("tkinter", "pytest", "_pytest", "py.test")
# passlib 按 scheme 名动态导入 handlers，uvicorn 按配置动态加载
# protocols/loops —— 静态分析抓不到，必须整包收集。
_COLLECT_SUBMODULES = ("proseforge", "uvicorn", "passlib")
_HIDDEN_IMPORTS = (
    # DBAPI 驱动在 dialect.import_dbapi() 内按需导入，静态分析抓不到。
    "aiosqlite",
    "asyncpg",
    "psycopg",
    "greenlet",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
    "sqlalchemy.dialects.postgresql.asyncpg",
    "sqlalchemy.dialects.postgresql.psycopg",
    "kombu.transport.redis",
    "multipart",
    "python_multipart",
)


class BundleError(RuntimeError):
    """Raised when the native bundle cannot be produced correctly."""


def host_target(platform_name: str) -> str:
    """Map a sys.platform value to a bundle target name."""
    key = platform_name.lower()
    if key.startswith("linux"):
        key = "linux"
    try:
        return _HOST_TARGETS[key]
    except KeyError:
        raise BundleError(f"unsupported host platform: {platform_name!r}") from None


def data_file_mappings(project_root: Path) -> list[tuple[Path, str]]:
    """(source, dest) pairs bundled via PyInstaller --add-data.

    dest is relative to the onedir ``_internal`` directory. Only build
    inputs live here — never user data, secrets, databases or caches.
    """
    root = Path(project_root)
    mappings = [
        (root / "apps" / "web" / "dist", "frontend-dist"),
        (root / "proseforge" / "infrastructure" / "database" / "migrations", "proseforge/infrastructure/database/migrations"),
        (root / "LICENSE", "."),
        (root / "VERSION", "."),
    ]
    # Offline embedding engine artifacts (packaging/models channel): included
    # only when fetch.py / fetch_llama_bin.py ran on the build host. They land
    # at _internal/packaging/models — the frozen app's default
    # embedding_cache_dir — so the bundle embeds offline out of the box.
    for subdir in ("gguf", "llama-bin"):
        source = root / "packaging" / "models" / subdir
        if source.is_dir() and any(source.iterdir()):
            mappings.append((source, f"packaging/models/{subdir}"))
    return mappings


def bundle_contents(project_root: Path, target: str) -> list[str]:
    """Manifest contents list, derived from data_file_mappings by construction."""
    executable = "proseforge.exe" if target == "windows" else "proseforge"
    contents = [executable]
    for source, dest in data_file_mappings(project_root):
        contents.append(f"_internal/{source.name}" if dest == "." else f"_internal/{dest}")
    contents += ["_internal/manifest.json", "manifest.json", "LICENSE"]
    return contents


def freeze_dependency_hashes(freeze_text: str) -> dict[str, str]:
    """Parse pip freeze output into {name: version} plus a combined sha256."""
    lines = sorted({line.strip() for line in freeze_text.splitlines() if "==" in line})
    mapping: dict[str, str] = {}
    for line in lines:
        name, _, version = line.partition("==")
        mapping[name.strip()] = version.strip()
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {**mapping, "combined_sha256": digest}


def _prune_caches(bundle_dir: Path) -> None:
    """删除随 --add-data 源码目录混入的 __pycache__（契约要求排除）。"""
    for path in sorted(bundle_dir.rglob("__pycache__"), reverse=True):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def rearrange_bundle(dist_dir: Path, bundle_dir: Path, *, manifest: dict, project_root: Path) -> Path:
    """Move PyInstaller's dist dir into the contract layout.

    ``dist/proseforge/`` becomes ``<output>/ProseForge/``; the manifest is
    written into ``_internal/`` (bundled view) and at the bundle root
    (installer/user readable), and LICENSE is copied to the root.
    """
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(dist_dir), str(bundle_dir))
    internal = bundle_dir / "_internal"
    if not internal.is_dir():
        raise BundleError(f"PyInstaller output lacks _internal directory: {bundle_dir}")
    _prune_caches(bundle_dir)
    write_manifest(internal / "manifest.json", manifest)
    write_manifest(bundle_dir / "manifest.json", manifest)
    shutil.copy2(Path(project_root) / "LICENSE", bundle_dir / "LICENSE")
    return bundle_dir


def _git_sha(project_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _capture(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise BundleError(f"command failed ({result.returncode}): {cmd!r}\n{result.stderr[-2000:]}")
    return result.stdout


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _ensure_build_venv(project_root: Path, target: str) -> Path:
    """Create (once) and populate the pinned Python 3.12 build venv."""
    venv_dir = project_root / f"{_BUILD_VENV_PREFIX}{target}"
    python = _venv_python(venv_dir)
    if not python.is_file():
        if sys.platform == "win32":
            create_cmd = ["py", "-3.12", "-m", "venv", str(venv_dir)]
        else:
            if sys.version_info[:2] != (3, 12):
                raise BundleError(
                    f"native builds require Python 3.12; current interpreter is {platform.python_version()}"
                )
            create_cmd = [sys.executable, "-m", "venv", str(venv_dir)]
        print("+", " ".join(create_cmd), flush=True)
        result = subprocess.run(create_cmd, cwd=project_root, check=False)
        if result.returncode:
            raise BundleError(f"failed to create build venv at {venv_dir}")
    venv_version = _capture(
        [str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"]
    ).strip()
    if venv_version != "3.12":
        raise BundleError(
            f"build venv {venv_dir} is Python {venv_version}, expected 3.12; delete it and retry"
        )
    install_cmd = [str(python), "-m", "pip", "install", "-q", "-e", f"{project_root}[api]", "pyinstaller"]
    print("+", " ".join(install_cmd), flush=True)
    result = subprocess.run(install_cmd, cwd=project_root, capture_output=True, text=True, check=False)
    if result.returncode:
        sys.stderr.write(result.stderr[-4000:])
        raise BundleError("failed to install build dependencies into the build venv")
    return python


_LICENSE_SERVICE_REL = Path("proseforge") / "application" / "licensing" / "service.py"
_PIN_ANCHOR = "BUILTIN_LICENSE_CENTER_PUBLIC_KEY: str | None = None"


def _inject_license_pin(staging: Path) -> None:
    """Inject the production license-center public key pin into the staged copy.

    PROSEFORGE_LICENSE_PIN_PUBLIC_KEY (64-char hex) replaces the None
    placeholder so shipped binaries verify the configured center key against
    a compiled-in constant. The repo working tree is never modified.
    """
    pin = os.environ.get("PROSEFORGE_LICENSE_PIN_PUBLIC_KEY", "").strip()
    if not pin:
        return
    if len(pin) != 64 or any(ch not in "0123456789abcdef" for ch in pin.lower()):
        raise BundleError("PROSEFORGE_LICENSE_PIN_PUBLIC_KEY must be 64 hex chars")
    service_py = staging / _LICENSE_SERVICE_REL
    text = service_py.read_text(encoding="utf-8")
    if _PIN_ANCHOR not in text:
        raise BundleError(f"license pin anchor missing in staged {_LICENSE_SERVICE_REL}")
    service_py.write_text(
        text.replace(_PIN_ANCHOR, f'BUILTIN_LICENSE_CENTER_PUBLIC_KEY: str | None = "{pin}"', 1),
        encoding="utf-8",
    )
    print("+ license pin injected into staged licensing service", flush=True)


def _stage_analysis_tree(project_root: Path, staging: Path) -> None:
    """Copy the importable build inputs (proseforge pkg, version, entry).

    The repo root must not land on PyInstaller's analysis path: its
    top-level ``packaging`` package would shadow the third-party
    ``packaging`` distribution during static analysis.
    """
    shutil.copy2(project_root / "version.py", staging / "version.py")
    shutil.copy2(project_root / "packaging" / "native_entry.py", staging / "native_entry.py")
    shutil.copytree(
        project_root / "proseforge",
        staging / "proseforge",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".env"),
    )
    _inject_license_pin(staging)


def _run_pyinstaller(python: Path, project_root: Path, staging: Path, build_root: Path) -> Path:
    separator = ";" if sys.platform == "win32" else ":"
    cmd = [
        str(python), "-m", "PyInstaller",
        "--name", "proseforge",
        "--onedir", "--clean", "--noconfirm",
        "--distpath", str(build_root / "dist"),
        "--workpath", str(build_root / "work"),
        "--specpath", str(build_root / "spec"),
        "--paths", str(staging),
    ]
    for package in _COLLECT_SUBMODULES:
        cmd += ["--collect-submodules", package]
    for module in _HIDDEN_IMPORTS:
        cmd += ["--hidden-import", module]
    for module in _EXCLUDED_MODULES:
        cmd += ["--exclude-module", module]
    for source, dest in data_file_mappings(project_root):
        cmd += ["--add-data", f"{source}{separator}{dest}"]
    cmd.append(str(staging / "native_entry.py"))
    print("+", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, cwd=project_root, check=False)
    if result.returncode:
        raise BundleError(f"PyInstaller failed with exit code {result.returncode}")
    dist_dir = build_root / "dist" / "proseforge"
    if not dist_dir.is_dir():
        raise BundleError(f"PyInstaller did not produce expected dist dir: {dist_dir}")
    return dist_dir


def _smoke_check(bundle_dir: Path, target: str, expected_version: str) -> None:
    executable = bundle_dir / ("proseforge.exe" if target == "windows" else "proseforge")
    if not executable.is_file():
        raise BundleError(f"bundle executable missing: {executable}")
    result = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, timeout=180, check=False
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0 or output != expected_version:
        raise BundleError(
            f"bundle smoke check failed: {executable.name} --version -> "
            f"rc={result.returncode} stdout={output!r} stderr={(result.stderr or '')[-2000:]!r}"
        )
    print(f"smoke check ok: {executable.name} --version -> {output}", flush=True)


def _write_archive(bundle_dir: Path, output_dir: Path, target: str, git_sha: str, archive_format: str) -> Path:
    suffix = _ARCHIVE_SUFFIX[archive_format]
    archive = output_dir / f"ProseForge-{target}-{git_sha[:12]}{suffix}"
    if archive.exists():
        archive.unlink()
    if archive_format == "zip":
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(bundle_dir.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(bundle_dir.parent))
    else:
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(bundle_dir, arcname=bundle_dir.name)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    # 显式 LF：Windows 文本模式默认换行转换会产生 CRLF，GNU sha256sum 无法解析。
    (output_dir / f"{archive.name}.sha256").write_text(
        f"{checksum}  {archive.name}\n", encoding="utf-8", newline="\n"
    )
    (output_dir / "BUILD.txt").write_text(
        f"archive={archive.name}\nsha256={checksum}\ntarget={target}\n",
        encoding="utf-8", newline="\n",
    )
    return archive


def build_bundle(*, project_root: Path, output_dir: Path, target: str, archive_format: str) -> Path:
    project_root = Path(project_root).resolve()
    output_dir = Path(output_dir).resolve()
    if archive_format not in _ARCHIVE_SUFFIX:
        raise BundleError(f"unsupported archive format: {archive_format!r}")
    if not (project_root / "apps" / "web" / "dist" / "index.html").is_file():
        raise BundleError(
            "frontend build missing: apps/web/dist/index.html — "
            "build the web frontend (apps/web) before packaging a native bundle"
        )
    host = host_target(sys.platform)
    if host != target:
        hint = (
            " Use scripts/build_native.sh --target linux to run the build "
            "inside a podman python:3.12 container." if target == "linux" else ""
        )
        raise BundleError(
            f"target {target!r} does not match host platform {host!r}; "
            f"refusing to produce a mismatched bundle.{hint}"
        )
    expected_version = get_version()
    git_sha = _git_sha(project_root)
    python = _ensure_build_venv(project_root, target)
    freeze_text = _capture([str(python), "-m", "pip", "freeze"])
    python_version = _capture(
        [str(python), "-c", "import platform; print(platform.python_version())"]
    ).strip()
    manifest = build_manifest(
        version=expected_version,
        git_sha=git_sha,
        target_os=target,
        python_version=python_version,
        dependency_hashes=freeze_dependency_hashes(freeze_text),
        contents=bundle_contents(project_root, target),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = output_dir / "ProseForge"
    archive: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="proseforge-pyi-") as temp:
            temp_root = Path(temp)
            staging = temp_root / "staging"
            staging.mkdir()
            _stage_analysis_tree(project_root, staging)
            dist_dir = _run_pyinstaller(python, project_root, staging, temp_root / "pyi")
            rearrange_bundle(dist_dir, bundle_dir, manifest=manifest, project_root=project_root)
        _smoke_check(bundle_dir, target, expected_version)
        archive = _write_archive(bundle_dir, output_dir, target, git_sha, archive_format)
    except Exception:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        if archive is not None and archive.exists():
            archive.unlink()
        raise
    shutil.copy2(bundle_dir / "manifest.json", output_dir / "manifest.json")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", choices=("windows", "linux", "macos"), required=True)
    parser.add_argument("--format", choices=("zip", "tar.gz"), default="tar.gz")
    args = parser.parse_args()
    archive = build_bundle(
        project_root=args.root,
        output_dir=args.output,
        target=args.target,
        archive_format=args.format,
    )
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
