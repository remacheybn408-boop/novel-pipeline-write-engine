"""Migration 0036 (model catalog modality cleanup) on sqlite.

Builds the model_catalog table directly, seeds dirty + clean rows, runs the
migration under an alembic operations context, and asserts the cleanup.
Also pins the migration regex to the domain rule set (they are manually
kept in sync — migrations must not import live domain code).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from proseforge.domain.model.modality import is_text_or_omni_model
from proseforge.infrastructure.database.models.remaining import ModelCatalogModel

_MIGRATION_PATH = Path(__file__).resolve().parents[2] / "proseforge/infrastructure/database/migrations/versions/0036_model_modality_cleanup.py"

_SEED = (
    ("1", "agnes", "agnes-image-2.0-flash"),  # dirty
    ("2", "agnes", "agnes-video-v2.0"),  # dirty
    ("3", "openai", "whisper-1"),  # dirty
    ("4", "openai", "text-embedding-3-large"),  # dirty
    ("5", "volcengine", "seedance-1.0"),  # dirty
    ("6", "agnes", "deepseek-chat"),  # clean
    ("7", "volcengine", "doubao-vision-pro-32k"),  # clean
    ("8", "openai", "gpt-4o"),  # clean
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0036", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def migrated(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    ModelCatalogModel.__table__.create(engine)
    with engine.begin() as conn:
        for row_id, provider, model_id in _SEED:
            conn.execute(sa.text("INSERT INTO model_catalog (id, provider, model_id, capabilities) VALUES (:i, :p, :m, '{}')"), {"i": row_id, "p": provider, "m": model_id})
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()
        conn.commit()
    return migration, engine


def test_upgrade_deletes_non_text_models_keeps_chat_models(migrated):
    _migration, engine = migrated
    with engine.connect() as conn:
        remaining = {row[0] for row in conn.execute(sa.text("SELECT model_id FROM model_catalog")).fetchall()}
    assert remaining == {"deepseek-chat", "doubao-vision-pro-32k", "gpt-4o"}


def test_migration_regex_matches_domain_rules():
    # The regex is a frozen copy of modality._EXCLUDE_PATTERNS; drift breaks
    # the cleanup silently, so pin the correspondence on a broad corpus.
    migration = _load_migration()
    compiled = re.compile(migration._EXCLUSION_REGEX, re.IGNORECASE)
    corpus = [row[2] for row in _SEED] + [
        "tts-1-hd", "dall-e-3", "doubao-embedding", "bge-m3", "kolors",
        "CosyVoice2", "seedream-3.0", "cogview-3", "rerank-v1",
        "deepseek-chat", "gpt-4o", "doubao-pro-32k", "doubao-vision-pro-32k",
        "qwen-vl-max", "glm-4v", "claude-sonnet-4", "gemini-2.5-pro",
        "qwen-omni-turbo", "yi-vision",
    ]
    for model_id in corpus:
        assert bool(compiled.search(model_id)) == (not is_text_or_omni_model(model_id)), model_id
