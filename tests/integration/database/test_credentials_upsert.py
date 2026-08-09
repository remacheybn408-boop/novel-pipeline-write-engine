import pytest

from proseforge.infrastructure.database.repositories.credential import (
    SqlAlchemyCredentialRepository,
)


@pytest.mark.asyncio
async def test_provider_credentials_upsert_without_creating_duplicates(session_factory):
    async with session_factory() as session:
        repository = SqlAlchemyCredentialRepository(session)
        first = await repository.upsert("u-settings", "openai", "encrypted-1")
        second = await repository.upsert("u-settings", "openai", "encrypted-2")
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyCredentialRepository(session)
        rows = await repository.list_for_user("u-settings")

    assert first.id == second.id
    assert len(rows) == 1
    assert rows[0].encrypted_payload == "encrypted-2"
