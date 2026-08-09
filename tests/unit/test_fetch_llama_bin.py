"""packaging/models/fetch_llama_bin.py: archive extraction and CLI defaults.
Nothing downloads — the release zip is faked locally.
"""

from __future__ import annotations

import io
import os
import stat
import tarfile
import zipfile
from pathlib import Path

from packaging.models import fetch_llama_bin


def _fake_release_tarball(path: Path, *, binary_name: str = "llama-server") -> Path:
    archive = path / "llama-b0000-bin-ubuntu-x64.tar.gz"
    entries = {
        f"build/bin/{binary_name}": b"fake-binary",
        "build/lib/libllama.so.0": b"fake-lib",
        "build/lib/libggml-base.so.0": b"fake-lib",
        "build/bin/README.md": b"not a lib",
    }
    with tarfile.open(archive, "w:gz") as bundle:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    return archive


def test_extract_binary_flattens_binary_and_shared_libs(tmp_path):
    archive = _fake_release_tarball(tmp_path)
    dest = tmp_path / "llama-bin"

    extracted = fetch_llama_bin.extract_binary(archive, "linux", dest)

    assert set(extracted) == {"llama-server", "libllama.so.0", "libggml-base.so.0"}
    assert (dest / "llama-server").read_bytes() == b"fake-binary"
    assert not (dest / "README.md").exists()
    if os.name != "nt":  # exec bits are a POSIX concept
        mode = (dest / "llama-server").stat().st_mode
        assert mode & stat.S_IXUSR  # executable bit set for the spawner


def test_extract_binary_windows_picks_exe_and_dlls(tmp_path):
    archive = tmp_path / "llama-b0000-bin-win-x64.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("llama-server.exe", b"fake-exe")
        bundle.writestr("ggml.dll", b"fake-dll")

    extracted = fetch_llama_bin.extract_binary(archive, "windows", tmp_path / "llama-bin")

    assert set(extracted) == {"llama-server.exe", "ggml.dll"}


def test_extract_binary_missing_binary_raises(tmp_path):
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("README.md", "nothing here")

    try:
        fetch_llama_bin.extract_binary(archive, "linux", tmp_path / "out")
    except RuntimeError as error:
        assert "llama-server not found" in str(error)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")


def test_parse_args_defaults():
    args = fetch_llama_bin.parse_args([])
    assert args.platform == "linux"
    assert args.tag == "latest"
    assert args.force is False


def test_platform_assets_match_runtime_convention():
    """The binary names must match what llama_server.llama_server_binary looks for."""
    assert fetch_llama_bin.PLATFORMS["linux"][1] == "llama-server"
    assert fetch_llama_bin.PLATFORMS["windows"][1] == "llama-server.exe"
    # Verified against the b10290 release asset list.
    assert fetch_llama_bin.PLATFORMS["linux"][0].endswith(".tar.gz")
    assert fetch_llama_bin.PLATFORMS["windows"][0].endswith(".zip")
    for asset, _binary, _suffixes in fetch_llama_bin.PLATFORMS.values():
        assert "{tag}" in asset


def test_main_skips_existing_binary(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "llama-bin"
    bin_dir.mkdir()
    (bin_dir / "llama-server").write_bytes(b"fake")
    monkeypatch.setattr(fetch_llama_bin, "BIN_DIR", bin_dir)

    assert fetch_llama_bin.main([]) == 0
    assert "[skip]" in capsys.readouterr().out
