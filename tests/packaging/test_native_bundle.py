from __future__ import annotations

import hashlib

import pytest

from packaging import native_bundle


def _fake_root(tmp_path, *, with_frontend: bool = True):
    root = tmp_path / "repo"
    (root / "apps" / "web").mkdir(parents=True)
    if with_frontend:
        dist = root / "apps" / "web" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "LICENSE").write_text("license text", encoding="utf-8")
    (root / "VERSION").write_text("1.5.0\n", encoding="utf-8")
    migrations = root / "proseforge" / "infrastructure" / "database" / "migrations" / "versions"
    migrations.mkdir(parents=True)
    (migrations / "0001_init.py").write_text("# migration", encoding="utf-8")
    return root


def test_data_file_mappings_cover_frontend_migrations_license(tmp_path):
    root = _fake_root(tmp_path)
    mappings = dict(native_bundle.data_file_mappings(root))
    sources = {src.name for src in mappings}
    assert "dist" in sources
    assert "migrations" in sources
    assert "LICENSE" in sources
    assert "VERSION" in sources
    dests = set(mappings.values())
    assert "frontend-dist" in dests
    assert "proseforge/infrastructure/database/migrations" in dests
    assert "." in dests
    rendered = " ".join(f"{src} {dest}" for src, dest in mappings.items())
    for forbidden in (".env", "__pycache__", "node_modules", ".sqlite", "exports"):
        assert forbidden not in rendered


def test_bundle_contents_match_mappings_and_layout(tmp_path):
    root = _fake_root(tmp_path)
    contents = native_bundle.bundle_contents(root, "windows")
    assert "proseforge.exe" in contents
    assert "_internal/frontend-dist" in contents
    assert "_internal/proseforge/infrastructure/database/migrations" in contents
    assert "_internal/LICENSE" in contents
    assert "_internal/manifest.json" in contents
    assert "manifest.json" in contents
    assert "LICENSE" in contents
    linux_contents = native_bundle.bundle_contents(root, "linux")
    assert "proseforge" in linux_contents
    assert "proseforge.exe" not in linux_contents


def test_embedding_artifacts_bundled_only_when_present(tmp_path):
    root = _fake_root(tmp_path)
    # Absent on the build host: no mapping, no manifest entry.
    assert not any(dest.startswith("packaging/models") for _src, dest in native_bundle.data_file_mappings(root))
    assert not any("packaging/models" in item for item in native_bundle.bundle_contents(root, "linux"))

    gguf = root / "packaging" / "models" / "gguf"
    gguf.mkdir(parents=True)
    (gguf / "bge-m3-Q8_0.gguf").write_bytes(b"fake-gguf")
    bin_dir = root / "packaging" / "models" / "llama-bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "llama-server").write_bytes(b"fake-binary")

    mappings = {str(src): dest for src, dest in native_bundle.data_file_mappings(root)}
    assert mappings[str(gguf)] == "packaging/models/gguf"
    assert mappings[str(bin_dir)] == "packaging/models/llama-bin"
    contents = native_bundle.bundle_contents(root, "linux")
    assert "_internal/packaging/models/gguf" in contents
    assert "_internal/packaging/models/llama-bin" in contents

    # An empty directory does not count as provisioned.
    empty = _fake_root(tmp_path / "second")
    (empty / "packaging" / "models" / "gguf").mkdir(parents=True)
    assert not any(dest.startswith("packaging/models") for _src, dest in native_bundle.data_file_mappings(empty))


def test_freeze_dependency_hashes_parses_mapping_and_combined_digest():
    freeze = "uvicorn==0.35.0\nfastapi==0.116.1\n# comment line\n\npydantic==2.11.0\n"
    hashes = native_bundle.freeze_dependency_hashes(freeze)
    assert hashes["uvicorn"] == "0.35.0"
    assert hashes["fastapi"] == "0.116.1"
    assert hashes["pydantic"] == "2.11.0"
    expected = hashlib.sha256(b"fastapi==0.116.1\npydantic==2.11.0\nuvicorn==0.35.0").hexdigest()
    assert hashes["combined_sha256"] == expected
    # 与输入行序无关
    shuffled = native_bundle.freeze_dependency_hashes("pydantic==2.11.0\nuvicorn==0.35.0\nfastapi==0.116.1\n")
    assert shuffled == hashes


def test_host_target_mapping():
    assert native_bundle.host_target("win32") == "windows"
    assert native_bundle.host_target("linux") == "linux"
    assert native_bundle.host_target("darwin") == "macos"
    with pytest.raises(native_bundle.BundleError):
        native_bundle.host_target("freebsd")


def test_build_bundle_requires_frontend_dist(tmp_path):
    root = _fake_root(tmp_path, with_frontend=False)
    with pytest.raises(native_bundle.BundleError, match="frontend|dist|apps/web"):
        native_bundle.build_bundle(
            project_root=root,
            output_dir=tmp_path / "out",
            target="windows",
            archive_format="zip",
        )


def test_build_bundle_rejects_platform_mismatch(tmp_path, monkeypatch):
    root = _fake_root(tmp_path)
    monkeypatch.setattr("sys.platform", "win32")
    with pytest.raises(native_bundle.BundleError, match="build_native\\.sh"):
        native_bundle.build_bundle(
            project_root=root,
            output_dir=tmp_path / "out",
            target="linux",
            archive_format="tar.gz",
        )
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(native_bundle.BundleError, match="mismatch|does not match|host"):
        native_bundle.build_bundle(
            project_root=root,
            output_dir=tmp_path / "out",
            target="windows",
            archive_format="zip",
        )


def test_rearrange_bundle_produces_contract_layout(tmp_path):
    root = _fake_root(tmp_path)
    dist = tmp_path / "dist" / "proseforge"
    internal = dist / "_internal"
    (internal / "frontend-dist").mkdir(parents=True)
    (internal / "frontend-dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    (internal / "LICENSE").write_text("license text", encoding="utf-8")
    stale_cache = internal / "proseforge" / "infrastructure" / "database" / "migrations" / "__pycache__"
    stale_cache.mkdir(parents=True)
    (stale_cache / "env.cpython-312.pyc").write_bytes(b"cached")
    (dist / "proseforge.exe").write_bytes(b"MZ-fake")
    manifest = {"version": "1.5.0", "git_sha": "abc123"}
    bundle = native_bundle.rearrange_bundle(
        dist, tmp_path / "out" / "ProseForge", manifest=manifest, project_root=root
    )
    assert bundle.name == "ProseForge"
    assert (bundle / "proseforge.exe").is_file()
    assert (bundle / "_internal" / "frontend-dist" / "index.html").is_file()
    assert (bundle / "_internal" / "manifest.json").is_file()
    assert (bundle / "manifest.json").is_file()
    assert (bundle / "LICENSE").read_text(encoding="utf-8") == "license text"
    assert not list(bundle.rglob("__pycache__"))
    assert not dist.exists()
