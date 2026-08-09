from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings


@pytest.mark.asyncio
async def test_uow_rolls_back_uncommitted_project():
    settings = Settings(
        database_url=os.environ.get(
            "PROSEFORGE_TEST_DATABASE_URL",
            "postgresql+asyncpg://proseforge:proseforge@postgres:5432/proseforge",
        ),
        redis_url=os.environ.get("PROSEFORGE_TEST_REDIS_URL", "redis://redis:6379/0"),
    )
    engine, session_factory = create_engine_and_sessionmaker(settings)
    table = f"uow_probe_{uuid4().hex[:12]}"
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE TABLE "{table}" (value TEXT NOT NULL)'))

    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.session.execute(text(f'INSERT INTO "{table}" (value) VALUES (:value)'), {"value": "lost"})

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            result = await uow.session.execute(text(f'SELECT COUNT(*) FROM "{table}"'))
            assert result.scalar_one() == 0
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
        await engine.dispose()
