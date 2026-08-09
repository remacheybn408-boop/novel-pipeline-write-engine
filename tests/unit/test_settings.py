from pathlib import Path

import pytest
from pydantic import ValidationError

from proseforge.settings import Settings


def test_production_rejects_placeholder_secrets():
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://x",
            redis_url="redis://x",
            master_key="replace-with-32-byte-base64-key",
            jwt_secret="replace-with-long-random-secret",
        )


def test_development_allows_local_defaults():
    settings = Settings(
        environment="development",
        database_url="postgresql+asyncpg://x",
        redis_url="redis://x",
    )
    assert settings.environment == "development"


def _dev_settings() -> Settings:
    return Settings(
        environment="development",
        database_url="postgresql+asyncpg://x",
        redis_url="redis://x",
    )


def test_embedding_cache_dir_defaults_to_repo_packaging_models(monkeypatch):
    monkeypatch.delenv("PROSEFORGE_EMBEDDING_CACHE_DIR", raising=False)

    settings = _dev_settings()

    expected = Path(__file__).resolve().parents[2] / "packaging" / "models"
    assert Path(settings.embedding_cache_dir) == expected


def test_embedding_cache_dir_env_override(monkeypatch):
    monkeypatch.setenv("PROSEFORGE_EMBEDDING_CACHE_DIR", "/custom/embeddings")

    assert _dev_settings().embedding_cache_dir == "/custom/embeddings"


def test_personas_dir_defaults_to_repo_packs_personas_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("PROSEFORGE_PERSONAS_DIR", raising=False)
    # Native installs run with a CWD outside the project root; the default
    # must resolve from the package location, not the CWD.
    monkeypatch.chdir(tmp_path)

    settings = _dev_settings()

    expected = Path(__file__).resolve().parents[2] / "packs" / "personas"
    assert Path(settings.personas_dir) == expected
    assert Path(settings.personas_dir).is_absolute()


def test_personas_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PROSEFORGE_PERSONAS_DIR", str(tmp_path))

    assert _dev_settings().personas_dir == str(tmp_path)


def _env_driven_settings() -> Settings:
    # No explicit environment kwarg: init values outrank env vars in
    # pydantic-settings, so the env-driven tests must omit it.
    return Settings(
        database_url="postgresql+asyncpg://x",
        redis_url="redis://x",
    )


def test_environment_env_var_canonical_name(monkeypatch):
    monkeypatch.delenv("PROSEFORGE_ENV", raising=False)
    monkeypatch.setenv("PROSEFORGE_ENVIRONMENT", "staging")

    assert _env_driven_settings().environment == "staging"


def test_environment_env_var_legacy_alias(monkeypatch):
    monkeypatch.delenv("PROSEFORGE_ENVIRONMENT", raising=False)
    monkeypatch.setenv("PROSEFORGE_ENV", "staging")

    assert _env_driven_settings().environment == "staging"


def test_production_env_var_triggers_security_validation(monkeypatch):
    monkeypatch.delenv("PROSEFORGE_ENV", raising=False)
    monkeypatch.setenv("PROSEFORGE_ENVIRONMENT", "production")

    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+asyncpg://x",
            redis_url="redis://x",
        )


def test_session_token_minutes_defaults_to_24_hours(monkeypatch):
    monkeypatch.delenv("PROSEFORGE_SESSION_TOKEN_MINUTES", raising=False)

    assert _dev_settings().session_token_minutes == 1440


def test_session_token_minutes_env_override(monkeypatch):
    monkeypatch.setenv("PROSEFORGE_SESSION_TOKEN_MINUTES", "30")

    assert _dev_settings().session_token_minutes == 30
