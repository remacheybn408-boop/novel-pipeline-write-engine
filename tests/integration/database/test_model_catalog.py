import pytest

from proseforge.domain.ports.model_provider import ProviderModel
from proseforge.infrastructure.database.repositories.model_catalog import (
    SqlAlchemyModelCatalogRepository,
)


@pytest.mark.asyncio
async def test_model_catalog_upsert_and_list_is_durable(session_factory):
    model = ProviderModel("openai", "gpt-test", "GPT Test", {"streaming": True}, context_window=128000)
    async with session_factory() as session:
        repository = SqlAlchemyModelCatalogRepository(session)
        await repository.upsert([model])
        await session.commit()

    async with session_factory() as session:
        rows = await SqlAlchemyModelCatalogRepository(session).list("openai")

    assert rows[0].model_id == "gpt-test"
    assert rows[0].context_window == 128000
    assert rows[0].capabilities["streaming"] is True
