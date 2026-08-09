import json
import platform
from pathlib import Path

from proseforge.runtime.factory import Runtime, create_runtime
from proseforge.runtime.profile import RuntimeCapabilities, RuntimeProfile
from proseforge.settings import Settings

INFO_KEYS = {
    "version",
    "profile",
    "platform",
    "database",
    "queue",
    "web_served_by",
    "data_dir_is_absolute",
}


def _native_settings(**overrides) -> Settings:
    base = {"runtime_profile": "native", "database_url": "sqlite:///native.db"}
    return Settings(**(base | overrides))


def test_create_runtime_returns_runtime():
    runtime = create_runtime(_native_settings())
    assert isinstance(runtime, Runtime)
    assert runtime.profile is RuntimeProfile.NATIVE
    assert runtime.capabilities == RuntimeCapabilities(database="sqlite", queue="local")


def test_capabilities_match_profile():
    cases = [
        (
            {"runtime_profile": "native", "database_url": "sqlite:///x.db"},
            RuntimeCapabilities("sqlite", "local"),
        ),
        ({"runtime_profile": "server"}, RuntimeCapabilities("postgresql", "celery")),
        ({"runtime_profile": "test"}, RuntimeCapabilities("test", "memory")),
    ]
    for kwargs, expected in cases:
        runtime = create_runtime(Settings(**kwargs))
        assert runtime.profile is RuntimeProfile(kwargs["runtime_profile"])
        assert runtime.capabilities == expected
        assert runtime.info["database"] == expected.database
        assert runtime.info["queue"] == expected.queue


def test_info_shape_and_platform():
    runtime = create_runtime(_native_settings())
    assert set(runtime.info) == INFO_KEYS
    assert isinstance(runtime.info["version"], str) and runtime.info["version"]
    assert runtime.info["profile"] == "native"
    assert runtime.info["platform"] == platform.system().lower()


def test_info_excludes_secrets(tmp_path):
    settings = _native_settings(
        data_dir=str(tmp_path),
        master_key="top-secret-master-key",
        jwt_secret="top-secret-jwt-value",
    )
    runtime = create_runtime(settings)
    payload = json.dumps(runtime.info)
    assert str(tmp_path) not in payload
    assert "top-secret-master-key" not in payload
    assert "top-secret-jwt-value" not in payload
    for key in runtime.info:
        for word in ("key", "secret", "password", "token", "host"):
            assert word not in key
    for value in runtime.info.values():
        if isinstance(value, str):
            assert not Path(value).is_absolute()


def test_data_dir_is_absolute_flag(tmp_path):
    absolute = create_runtime(_native_settings(data_dir=str(tmp_path)))
    assert absolute.info["data_dir_is_absolute"] is True
    relative = create_runtime(_native_settings(data_dir="data"))
    assert relative.info["data_dir_is_absolute"] is False
    unset = create_runtime(_native_settings())
    assert unset.info["data_dir_is_absolute"] is False


def test_web_served_by():
    served = create_runtime(_native_settings(serve_web=True))
    assert served.info["web_served_by"] == "api"
    external = create_runtime(_native_settings(serve_web=False))
    assert external.info["web_served_by"] == "external"


def test_create_app_attaches_runtime():
    from proseforge.api.main import create_app

    app = create_app(Settings())
    assert isinstance(app.state.runtime, Runtime)
    assert app.state.runtime.profile is RuntimeProfile.SERVER
    assert app.state.runtime.capabilities == RuntimeCapabilities("postgresql", "celery")
