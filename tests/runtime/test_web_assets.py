from __future__ import annotations

import sys

from proseforge.runtime.web_assets import locate_frontend_dir


def _package_file(root) -> str:
    """模拟打包/仓库布局下 proseforge/runtime/web_assets.py 的位置（无需真实存在）。"""
    return str(root / "proseforge" / "runtime" / "web_assets.py")


def test_env_override_wins(tmp_path, monkeypatch):
    frontend = tmp_path / "env-frontend"
    frontend.mkdir()
    bundle = tmp_path / "bundle"
    (bundle / "frontend-dist").mkdir(parents=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    result = locate_frontend_dir({"PROSEFORGE_FRONTEND_DIR": str(frontend)}, package_file=_package_file(tmp_path))
    assert result == frontend.resolve()


def test_env_override_ignored_when_not_a_directory(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    frontend = tmp_path / "apps" / "web" / "dist"
    frontend.mkdir(parents=True)
    result = locate_frontend_dir({"PROSEFORGE_FRONTEND_DIR": str(tmp_path / "missing")}, package_file=_package_file(tmp_path))
    assert result == frontend.resolve()


def test_meipass_bundle_beats_package_adjacent(tmp_path, monkeypatch):
    bundle = tmp_path / "meipass"
    frontend = bundle / "frontend-dist"
    frontend.mkdir(parents=True)
    (tmp_path / "frontend-dist").mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    result = locate_frontend_dir({}, package_file=_package_file(tmp_path))
    assert result == frontend.resolve()


def test_package_adjacent_frontend_dist_beats_repo_layout(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    frontend = tmp_path / "frontend-dist"
    frontend.mkdir()
    (tmp_path / "apps" / "web" / "dist").mkdir(parents=True)
    result = locate_frontend_dir({}, package_file=_package_file(tmp_path))
    assert result == frontend.resolve()


def test_repo_dev_layout(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    frontend = tmp_path / "apps" / "web" / "dist"
    frontend.mkdir(parents=True)
    result = locate_frontend_dir({}, package_file=_package_file(tmp_path))
    assert result == frontend.resolve()


def test_returns_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert locate_frontend_dir({}, package_file=_package_file(tmp_path)) is None
