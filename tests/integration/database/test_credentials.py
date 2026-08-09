import pytest

from proseforge.infrastructure.database.repositories.credential import (
    SqlAlchemyCredentialRepository,
)


@pytest.mark.asyncio
async def test_credentials_are_owner_scoped(session_factory):
    async with session_factory() as session:
        repository = SqlAlchemyCredentialRepository(session)
        record = await repository.create("u1", "openai", "encrypted")
        await session.commit()

    async with session_factory() as session:
        rows = await SqlAlchemyCredentialRepository(session).list_for_user("u1")
        other = await SqlAlchemyCredentialRepository(session).list_for_user("u2")

    assert rows[0].id == record.id
    assert rows[0].encrypted_payload == "encrypted"
    assert other == []
