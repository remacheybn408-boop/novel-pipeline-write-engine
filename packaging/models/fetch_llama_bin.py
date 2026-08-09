#!/usr/bin/env python3
"""Fetch the llama.cpp ``llama-server`` binary into ./llama-bin/.

Run once (online) before building an offline distribution or Docker image:

    python packaging/models/fetch_llama_bin.py                  # linux x86_64, latest release
    python packaging/models/fetch_llama_bin.py --tag b7006      # pinned release
    python packaging/models/fetch_llama_bin.py --platform windows

The layout matches the runtime convention in
proseforge/infrastructure/embeddings/llama_server.py (LLAMA_BIN_DIR):
``llama-bin/llama-server`` (or ``llama-server.exe``) with the release's
shared libraries extracted flat alongside it — the spawner puts the binary
directory on LD_LIBRARY_PATH, so no system install is needed.

Stdlib only (the slim Docker images run this at build time). Idempotent:
an existing binary is skipped unless --force is given.
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = "ggml-org/llama.cpp"
RELEASES = f"https://github.com/{REPO}/releases"

CACHE_DIR = Path(__file__).resolve().parent
BIN_DIR = CACHE_DIR / "llama-bin"

# platform -> (asset name template, binary name, shared-lib suffixes).
# Asset names verified against the b10290 release: linux ships as tar.gz,
# windows as the CPU-only zip (the CUDA/Vulkan variants need vendor GPUs).
# The GGUF registry lives in llama_server.LLAMA_MODELS; only the binary
# side is provisioned here.
PLATFORMS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "linux": ("llama-{tag}-bin-ubuntu-x64.tar.gz", "llama-server", (".so",)),
    "windows": ("llama-{tag}-bin-win-cpu-x64.zip", "llama-server.exe", (".dll",)),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_latest_tag() -> str:
    """Resolve the latest release tag via the /releases/latest redirect."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(f"{RELEASES}/latest", timeout=30)
    except urllib.error.HTTPError as error:
        if error.code in (301, 302, 303, 307, 308):
            location = error.headers.get("Location", "")
            tag = location.rstrip("/").rsplit("/", 1)[-1]
            if tag:
                return tag
        raise
    raise RuntimeError("could not resolve the latest llama.cpp release tag")


def _is_shared_lib(name: str, suffixes: tuple[str, ...]) -> bool:
    return any(suffix in name for suffix in suffixes)


def _archive_members(archive: Path) -> tuple[object, list[str], object]:
    """Open a zip or tar.gz release archive; returns (handle, names, open_member).
    The caller closes the handle (contextlib.closing)."""
    if archive.name.endswith(".zip"):
        bundle = zipfile.ZipFile(archive)
        return bundle, bundle.namelist(), bundle.open
    bundle = tarfile.open(archive, "r:gz")  # noqa: SIM115 - closed by the caller
    names = bundle.getnames()
    return bundle, names, bundle.extractfile


def extract_binary(archive: Path, platform_name: str, dest: Path) -> list[str]:
    """Extract the llama-server binary and its shared libs flat into dest."""
    _asset, binary_name, lib_suffixes = PLATFORMS[platform_name]
    extracted: list[str] = []
    bundle, names, open_member = _archive_members(archive)
    with contextlib.closing(bundle):
        binary_entries = [name for name in names if name.rsplit("/", 1)[-1] == binary_name]
        if not binary_entries:
            raise RuntimeError(f"{binary_name} not found in {archive.name}: {names[:10]} ...")
        picks = [binary_entries[0]] + [
            name for name in names if _is_shared_lib(name.rsplit("/", 1)[-1], lib_suffixes)
        ]
        dest.mkdir(parents=True, exist_ok=True)
        for entry in picks:
            member = open_member(entry)
            if member is None:  # tar: non-file member (dir/symlink)
                continue
            target = dest / entry.rsplit("/", 1)[-1]
            with member as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)
            extracted.append(target.name)
    binary_path = dest / binary_name
    binary_path.chmod(binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return extracted


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform", choices=sorted(PLATFORMS), default="linux")
    parser.add_argument("--tag", default="latest", help="llama.cpp release tag (e.g. b7006) or 'latest'")
    parser.add_argument("--force", action="store_true", help="re-download even if the binary exists")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    asset_template, binary_name, _suffixes = PLATFORMS[args.platform]
    target = BIN_DIR / binary_name
    if target.is_file() and not args.force:
        print(f"[skip] {target} already present ({target.stat().st_size / 1e6:.1f} MB)")
        return 0
    tag = resolve_latest_tag() if args.tag == "latest" else args.tag
    asset = asset_template.format(tag=tag)
    url = f"{RELEASES}/download/{tag}/{asset}"
    print(f"[fetch] {url}")
    with tempfile.TemporaryDirectory(prefix="llama-bin-") as temp:
        archive = Path(temp) / asset
        urllib.request.urlretrieve(url, archive)
        extracted = extract_binary(archive, args.platform, BIN_DIR)
    print(f"[done] {BIN_DIR}: {', '.join(extracted)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
