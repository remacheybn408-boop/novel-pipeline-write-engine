"""User-level key/value preferences (embedding config, ...)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.plugin import UserPreferenceModel


class SqlAlchemyUserPreferenceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, user_id: str, key: str) -> UserPreferenceModel | None:
        return await self.session.scalar(
            select(UserPreferenceModel).where(
                UserPreferenceModel.user_id == user_id,
                UserPreferenceModel.key == key,
            )
        )

    async def set(self, user_id: str, key: str, value_json: str) -> UserPreferenceModel:
        now = datetime.now(UTC)
        values = {"id": new_id(), "user_id": user_id, "key": key, "value_json": value_json, "updated_at": now}
        bind = self.session.bind
        if bind is not None and bind.dialect.name == "postgresql":
            statement = pg_insert(UserPreferenceModel).values(**values).on_conflict_do_update(
                constraint="uq_user_preferences_user_key",
                set_={"value_json": value_json, "updated_at": now},
            )
            await self.session.execute(statement)
        elif bind is not None and bind.dialect.name == "sqlite":
            statement = sqlite_insert(UserPreferenceModel).values(**values).on_conflict_do_update(
                index_elements=("user_id", "key"),
                set_={"value_json": value_json, "updated_at": now},
            )
            await self.session.execute(statement)
        else:
            existing = await self.get(user_id, key)
            if existing is None:
                self.session.add(UserPreferenceModel(**values))
            else:
                existing.value_json = value_json
                existing.updated_at = now
        await self.session.flush()
        preference = await self.get(user_id, key)
        if preference is None:
            raise RuntimeError("user preference upsert did not persist a record")
        return preference
