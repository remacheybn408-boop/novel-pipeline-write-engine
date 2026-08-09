"""Indexing window per engine model (第 15 项 按模型定窗): the local
engine's chunk window comes from the registry's chunk_chars instead of the
historic fixed 450, so bge-m3's recall advantage is not eaten by chunking.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.retrieval.indexing import (
    EMBEDDING_PREF_KEY,
    OFF_IDENTITY,
    _resolve_embedding_engine,
)
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.plugin import UserPreferenceModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.embeddings.llama_server import LlamaServerEmbedder
from proseforge.infrastructure.embeddings.local import LocalEmbedder
from tests.conftest import make_fk_engine

_TABLES = [UserModel.__table__, UserPreferenceModel.__table__]


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(UserModel(id="u1", email="u@example.com", password_hash="x", role="USER", session_version=1))
        await session.commit()
    yield factory
    await engine.dispose()


async def _set_preference(factory, value: dict | None) -> None:
    async with factory() as session:
        row = await session.get(UserPreferenceModel, "pref-1")
        if value is None:
            if row is not None:
                await session.delete(row)
        elif row is None:
            session.add(UserPreferenceModel(
                id="pref-1", user_id="u1", key=EMBEDDING_PREF_KEY,
                value_json=json.dumps(value), updated_at=datetime.now(UTC),
            ))
        else:
            row.value_json = json.dumps(value)
            row.updated_at = datetime.now(UTC)
        await session.commit()


async def _resolve(factory):
    async with SqlAlchemyUnitOfWork(factory) as uow:
        return await _resolve_embedding_engine(uow, "u1", "master-key-not-used-for-local")


@pytest.mark.asyncio()
async def test_default_preference_uses_bge_m3_window(session_factory):
    await _set_preference(session_factory, None)
    engine = await _resolve(session_factory)
    assert engine is not None
    assert engine.kind == "local"
    assert engine.identity == "local/BAAI/bge-m3"
    assert isinstance(engine.embedder, LlamaServerEmbedder)
    assert engine.max_chars == 1200


@pytest.mark.asyncio()
async def test_bge_m3_preference_gets_1200_char_window(session_factory):
    await _set_preference(session_factory, {"engine": "local", "local_model": "BAAI/bge-m3"})
    engine = await _resolve(session_factory)
    assert engine is not None
    assert engine.max_chars == 1200
    # Registry dimension guard (indexing.py) pins the same 1024 the
    # vector(1024) migration enforces.
    assert engine.embedder is not None and engine.embedder.dimension == 1024


@pytest.mark.asyncio()
async def test_hidden_512_token_model_keeps_450_char_window(session_factory):
    """Rollback path: a hidden fastembed model still resolves, with its own
    registry window (512 tokens -> 450 chars), not the bge-m3 one."""
    await _set_preference(session_factory, {"engine": "local", "local_model": "BAAI/bge-small-zh-v1.5"})
    engine = await _resolve(session_factory)
    assert engine is not None
    assert isinstance(engine.embedder, LocalEmbedder)
    assert engine.max_chars == 450


@pytest.mark.asyncio()
async def test_off_engine_keeps_default_window(session_factory):
    await _set_preference(session_factory, {"engine": "off"})
    engine = await _resolve(session_factory)
    assert engine is not None
    assert engine.identity == OFF_IDENTITY
    assert engine.max_chars == 700
