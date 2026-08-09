from __future__ import annotations

import json

from packaging.manifest import build_manifest, write_manifest


def test_manifest_contains_reproducibility_fields_and_no_runtime_data():
    manifest = build_manifest(version="1.5.0", git_sha="abc123", target_os="linux", arch="x86_64", python_version="3.12")
    assert manifest["version"] == "1.5.0"
    assert manifest["git_sha"] == "abc123"
    assert manifest["target"]["os"] == "linux"
    assert manifest["target"]["arch"] == "x86_64"
    assert "database" not in str(manifest).lower()
    assert "api_key" not in str(manifest).lower()


def test_manifest_required_fields_default_version_and_no_secrets(tmp_path):
    contents = ["proseforge.exe", "_internal/frontend-dist", "manifest.json", "LICENSE"]
    manifest = build_manifest(
        git_sha="32ca7dbbcbc31d84e64805e0c524a6ca9b48ec06",
        target_os="windows",
        dependency_hashes={"uvicorn": "0.35.0", "combined_sha256": "deadbeef"},
        contents=contents,
    )
    for field in ("version", "git_sha", "python_version", "target", "build_time", "dependency_hashes", "contents"):
        assert field in manifest
    assert manifest["version"] == "1.5.0"  # 来自仓库根 VERSION 文件
    assert manifest["git_sha"]
    assert manifest["dependency_hashes"]
    assert manifest["contents"] == contents
    blob = json.dumps(manifest).lower()
    for sensitive in ("api_key", "secret", "password", "token", ".env"):
        assert sensitive not in blob
    out = tmp_path / "manifest.json"
    write_manifest(out, manifest)
    assert json.loads(out.read_text(encoding="utf-8")) == manifest

