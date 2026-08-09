import pytest
from pydantic import ValidationError

from proseforge.runtime.profile import (
    RuntimeCapabilities,
    RuntimeProfile,
    capabilities_for,
)
from proseforge.settings import Settings


def test_default_profile_is_server():
    settings = Settings()
    assert settings.runtime_profile is RuntimeProfile.SERVER


def test_default_native_profile():
    settings = Settings(runtime_profile="native", database_url="sqlite:///native.db")
    assert settings.runtime_profile is RuntimeProfile.NATIVE
    capabilities = capabilities_for(settings.runtime_profile)
    assert capabilities == RuntimeCapabilities(database="sqlite", queue="local")


def test_server_rejects_native_defaults():
    with pytest.raises(ValidationError):
        Settings(runtime_profile="server", database_url="sqlite:///fallback.db")


def test_native_rejects_postgres_url(monkeypatch):
    monkeypatch.delenv("PROSEFORGE_ALLOW_NATIVE_POSTGRES", raising=False)
    with pytest.raises(ValidationError):
        Settings(runtime_profile="native")


def test_native_allows_postgres_with_override(monkeypatch):
    monkeypatch.setenv("PROSEFORGE_ALLOW_NATIVE_POSTGRES", "true")
    settings = Settings(runtime_profile="native")
    assert settings.runtime_profile is RuntimeProfile.NATIVE


def test_invalid_profile_rejected():
    with pytest.raises(ValidationError):
        Settings(runtime_profile="bogus")


def test_capability_mapping():
    assert capabilities_for(RuntimeProfile.NATIVE) == RuntimeCapabilities("sqlite", "local")
    assert capabilities_for(RuntimeProfile.SERVER) == RuntimeCapabilities(
        "postgresql", "celery"
    )
    assert capabilities_for(RuntimeProfile.TEST) == RuntimeCapabilities("test", "memory")


def test_runtime_field_defaults():
    settings = Settings()
    assert settings.data_dir is None
    assert settings.frontend_dir is None
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.serve_web is False
    assert settings.native_queue_poll_seconds == 1.0
    assert settings.native_worker_concurrency == 2
