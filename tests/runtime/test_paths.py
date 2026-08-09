"""V15-002：平台数据目录解析与 bootstrap 的契约测试。"""

from pathlib import PurePosixPath, PureWindowsPath

import pytest

from proseforge.runtime.bootstrap import bootstrap_runtime
from proseforge.runtime.paths import RuntimePaths, resolve_paths
from proseforge.runtime.profile import RuntimeProfile

CONTAINER_HOSTNAMES = ("postgres", "redis")


def _platform_env(**overrides: str) -> dict[str, str]:
    env = {
        "HOME": "/home/tester",
        "USERPROFILE": "C:\\Users\\tester",
        "LOCALAPPDATA": "C:\\Users\\tester\\AppData\\Local",
    }
    env.update(overrides)
    return env


def _all_paths(paths: RuntimePaths) -> list:
    candidates = [
        paths.data_dir,
        paths.database_path,
        paths.blob_dir,
        paths.backup_dir,
        paths.log_dir,
        paths.frontend_dir,
    ]
    return [p for p in candidates if p is not None]


def test_native_win32_defaults():
    paths = resolve_paths(RuntimeProfile.NATIVE, _platform_env(), platform="win32")
    root = PureWindowsPath("C:\\Users\\tester\\AppData\\Local\\ProseForge")
    assert paths.data_dir == root
    assert paths.database_path == root / "proseforge.sqlite3"
    assert paths.blob_dir == root / "blobs"
    assert paths.backup_dir == root / "backups"
    assert paths.log_dir == root / "logs"
    assert paths.frontend_dir is None


def test_native_darwin_defaults():
    paths = resolve_paths(RuntimeProfile.NATIVE, _platform_env(), platform="darwin")
    root = PurePosixPath("/home/tester/Library/Application Support/ProseForge")
    assert paths.data_dir == root
    assert paths.database_path == root / "proseforge.sqlite3"
    assert paths.blob_dir == root / "blobs"
    assert paths.backup_dir == root / "backups"
    assert paths.log_dir == root / "logs"


def test_native_linux_defaults_with_xdg():
    env = _platform_env(XDG_DATA_HOME="/home/tester/.xdg")
    paths = resolve_paths(RuntimeProfile.NATIVE, env, platform="linux")
    assert paths.data_dir == PurePosixPath("/home/tester/.xdg/ProseForge")
    assert paths.database_path == paths.data_dir / "proseforge.sqlite3"


def test_native_linux_defaults_without_xdg():
    paths = resolve_paths(RuntimeProfile.NATIVE, _platform_env(), platform="linux")
    assert paths.data_dir == PurePosixPath("/home/tester/.local/share/ProseForge")


def test_explicit_data_dir_overrides_platform_default():
    env = _platform_env(PROSEFORGE_DATA_DIR="D:\\PFData")
    paths = resolve_paths(RuntimeProfile.NATIVE, env, platform="win32")
    assert paths.data_dir == PureWindowsPath("D:\\PFData")
    assert paths.blob_dir == paths.data_dir / "blobs"
    assert paths.backup_dir == paths.data_dir / "backups"
    assert paths.log_dir == paths.data_dir / "logs"


def test_explicit_blob_backup_frontend_overrides():
    env = _platform_env(
        PROSEFORGE_DATA_DIR="/srv/pf",
        PROSEFORGE_BLOB_ROOT="/srv/blobs-x",
        PROSEFORGE_BACKUP_ROOT="/srv/backups-x",
        PROSEFORGE_FRONTEND_DIR="/srv/frontend",
    )
    paths = resolve_paths(RuntimeProfile.NATIVE, env, platform="linux")
    assert paths.blob_dir == PurePosixPath("/srv/blobs-x")
    assert paths.backup_dir == PurePosixPath("/srv/backups-x")
    assert paths.frontend_dir == PurePosixPath("/srv/frontend")
    assert paths.log_dir == PurePosixPath("/srv/pf/logs")


def test_explicit_sqlite_database_url_overrides_generated_path():
    env = _platform_env(
        PROSEFORGE_DATA_DIR="/srv/pf",
        PROSEFORGE_DATABASE_URL="sqlite:///custom.db",
    )
    paths = resolve_paths(RuntimeProfile.NATIVE, env, platform="linux")
    assert paths.database_path == PurePosixPath("/srv/pf/custom.db")


def test_postgres_database_url_yields_no_local_database_path():
    env = _platform_env(
        PROSEFORGE_DATABASE_URL="postgresql+asyncpg://u:p@postgres:5432/proseforge"
    )
    paths = resolve_paths(RuntimeProfile.NATIVE, env, platform="linux")
    assert paths.database_path is None


def test_in_memory_sqlite_url_yields_no_database_path():
    env = _platform_env(PROSEFORGE_DATABASE_URL="sqlite:///:memory:")
    paths = resolve_paths(RuntimeProfile.NATIVE, env, platform="linux")
    assert paths.database_path is None


def test_server_profile_uses_container_defaults_without_sqlite():
    paths = resolve_paths(RuntimeProfile.SERVER, _platform_env(), platform="linux")
    assert paths.database_path is None
    assert paths.data_dir == PurePosixPath("/data")
    assert paths.blob_dir == PurePosixPath("/data/blobs")
    assert paths.backup_dir == PurePosixPath("/data/backups")
    assert paths.log_dir == PurePosixPath("/data/logs")


def test_server_profile_honours_settings_env_fields():
    env = _platform_env(
        PROSEFORGE_BLOB_ROOT="/mnt/blobs",
        PROSEFORGE_BACKUP_ROOT="/mnt/backups",
        PROSEFORGE_DATABASE_URL="postgresql://u:p@postgres:5432/proseforge",
        PROSEFORGE_REDIS_URL="redis://redis:6379/0",
    )
    paths = resolve_paths(RuntimeProfile.SERVER, env, platform="linux")
    assert paths.database_path is None
    assert paths.blob_dir == PurePosixPath("/mnt/blobs")
    assert paths.backup_dir == PurePosixPath("/mnt/backups")


def test_unknown_platform_raises():
    with pytest.raises(ValueError, match="platform"):
        resolve_paths(RuntimeProfile.NATIVE, _platform_env(), platform="amigaos")


def test_test_profile_requires_explicit_data_dir():
    with pytest.raises(ValueError, match="PROSEFORGE_DATA_DIR"):
        resolve_paths(RuntimeProfile.TEST, _platform_env(), platform="linux")


def test_test_profile_uses_provided_temp_dir(tmp_path):
    env = _platform_env(PROSEFORGE_DATA_DIR=str(tmp_path))
    paths = resolve_paths(RuntimeProfile.TEST, env)
    assert paths.data_dir == tmp_path
    assert paths.database_path == tmp_path / "proseforge.sqlite3"
    assert paths.blob_dir == tmp_path / "blobs"


@pytest.mark.parametrize("platform_name", ["win32", "darwin", "linux"])
def test_all_returned_paths_are_absolute(platform_name):
    paths = resolve_paths(RuntimeProfile.NATIVE, _platform_env(), platform=platform_name)
    for path in _all_paths(paths):
        assert path.is_absolute(), f"{path} is not absolute"


def test_no_container_hostnames_in_any_path():
    env = _platform_env(
        PROSEFORGE_DATABASE_URL="postgresql+asyncpg://u:p@postgres:5432/proseforge",
        PROSEFORGE_REDIS_URL="redis://redis:6379/0",
        PROSEFORGE_BLOB_ROOT="/mnt/blobs",
        PROSEFORGE_BACKUP_ROOT="/mnt/backups",
    )
    for profile in (RuntimeProfile.NATIVE, RuntimeProfile.SERVER):
        paths = resolve_paths(profile, env, platform="linux")
        for path in _all_paths(paths):
            lowered = str(path).lower()
            for hostname in CONTAINER_HOSTNAMES:
                assert hostname not in lowered


def test_bootstrap_native_creates_directories(tmp_path):
    env = _platform_env(PROSEFORGE_DATA_DIR=str(tmp_path / "pf"))
    paths = resolve_paths(RuntimeProfile.NATIVE, env)
    result = bootstrap_runtime(paths, RuntimeProfile.NATIVE)
    assert result.skipped is False
    assert set(result.ensured_dirs) == {
        paths.data_dir,
        paths.blob_dir,
        paths.backup_dir,
        paths.log_dir,
    }
    for directory in result.ensured_dirs:
        assert directory.is_dir()


def test_bootstrap_server_is_noop(tmp_path):
    target = tmp_path / "should-not-be-created"
    env = _platform_env(
        PROSEFORGE_DATA_DIR=str(target),
        PROSEFORGE_BLOB_ROOT=str(target / "blobs"),
        PROSEFORGE_BACKUP_ROOT=str(target / "backups"),
    )
    paths = resolve_paths(RuntimeProfile.SERVER, env)
    result = bootstrap_runtime(paths, RuntimeProfile.SERVER)
    assert result.skipped is True
    assert result.ensured_dirs == ()
    assert not target.exists()
