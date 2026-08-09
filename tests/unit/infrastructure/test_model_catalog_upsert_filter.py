"""Catalog upsert modality filter (sqlite in-memory via aiosqlite).

The upsert choke point must drop non-text models, stamp vision on omni
models without clobbering provider values, and apply the same filter to
manual registrations.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.domain.ports.model_provider import ProviderModel
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.remaining import ModelCatalogModel
from proseforge.infrastructure.database.repositories.model_catalog import (
    SqlAlchemyModelCatalogRepository,
)
from tests.conftest import make_fk_engine


@pytest_asyncio.fixture
async def session_factory():
    engine = make_fk_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # users first: model_catalog.owner_id FKs it (migration 0050), and
        # the pragma-enabled engine rejects inserts with a missing parent.
        await conn.run_sync(UserModel.__table__.create)
        await conn.run_sync(ModelCatalogModel.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _model(model_id: str, capabilities: dict | None = None) -> ProviderModel:
    return ProviderModel("agnes", model_id, model_id, capabilities or {})


@pytest.mark.asyncio
async def test_upsert_keeps_only_text_and_omni_models(session_factory):
    async with session_factory() as session:
        repo = SqlAlchemyModelCatalogRepository(session)
        await repo.upsert([
            _model("deepseek-chat"),
            _model("doubao-vision-pro-32k"),
            _model("agnes-image-2.0-flash"),
            _model("agnes-video-v2.0"),
            _model("whisper-1"),
            _model("text-embedding-3-large"),
        ])
        await session.commit()
    async with session_factory() as session:
        kept = {model.model_id: model for model in await SqlAlchemyModelCatalogRepository(session).list()}
    assert set(kept) == {"deepseek-chat", "doubao-vision-pro-32k"}
    # Omni model got the vision stamp...
    assert kept["doubao-vision-pro-32k"].capabilities.get("vision") is True
    # ...and the text-only model did not.
    assert "vision" not in kept["deepseek-chat"].capabilities


@pytest.mark.asyncio
async def test_upsert_does_not_clobber_provider_vision_value(session_factory):
    async with session_factory() as session:
        repo = SqlAlchemyModelCatalogRepository(session)
        await repo.upsert([_model("doubao-vision-pro-32k", {"vision": False})])
        await session.commit()
    async with session_factory() as session:
        model = await SqlAlchemyModelCatalogRepository(session).get("agnes", "doubao-vision-pro-32k")
    assert model is not None and model.capabilities.get("vision") is False


@pytest.mark.asyncio
async def test_manual_registration_is_filtered_too(session_factory):
    async with session_factory() as session:
        repo = SqlAlchemyModelCatalogRepository(session)
        await repo.upsert([
            _model("my-image-gen", {"manual": True}),
            _model("my-chat-model", {"manual": True}),
        ])
        await session.commit()
    async with session_factory() as session:
        kept = {model.model_id for model in await SqlAlchemyModelCatalogRepository(session).list()}
    assert kept == {"my-chat-model"}
