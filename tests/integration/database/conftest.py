from __future__ import annotations

import os

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.settings import Settings


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    settings = Settings(
        database_url=os.environ.get(
            "PROSEFORGE_TEST_DATABASE_URL",
            "postgresql+asyncpg://proseforge:proseforge@postgres:5432/proseforge",
        ),
        redis_url=os.environ.get("PROSEFORGE_TEST_REDIS_URL", "redis://redis:6379/0"),
    )
    engine, factory = create_engine_and_sessionmaker(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
