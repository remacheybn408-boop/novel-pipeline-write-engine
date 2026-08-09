import pytest

from proseforge.domain.usage import UsageDelta
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.repositories.usage import (
    SqlAlchemyUsageRepository,
)


@pytest.mark.asyncio
async def test_usage_records_are_durable_and_idempotent(session_factory):
    async with session_factory() as session:
        session.add(ProjectModel(id="p-usage", owner_id="u1", slug="usage", title="Usage"))
        await session.flush()
        repository = SqlAlchemyUsageRepository(session)
        first = await repository.record(
            user_id="u1", project_id="p-usage", provider="google", model_id="gemini-test", call_id="call-1",
            delta=UsageDelta(input_tokens=4, output_tokens=3, total_tokens=7, final=False),
        )
        second = await repository.record(
            user_id="u1", project_id="p-usage", provider="google", model_id="gemini-test", call_id="call-1",
            delta=UsageDelta(input_tokens=5, output_tokens=4, total_tokens=9, final=True),
        )
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyUsageRepository(session)
        rows = await repository.list_for_user("u1", project_id="p-usage")

    assert first.id == second.id
    assert len(rows) == 1
    assert rows[0].total_tokens == 9
    assert rows[0].is_final is True
