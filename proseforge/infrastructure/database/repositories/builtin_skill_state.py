from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.domain.plugin.entity import BuiltinSkillState
from proseforge.infrastructure.database.models.plugin import UserBuiltinSkillStateModel


class SqlAlchemyBuiltinSkillStateRepository:
    """内置 skill 的每用户启用状态；无状态行时调用方按默认 disabled 处理。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_for_user(self, user_id: str) -> list[BuiltinSkillState]:
        rows = await self.session.scalars(
            select(UserBuiltinSkillStateModel).where(UserBuiltinSkillStateModel.user_id == user_id).order_by(UserBuiltinSkillStateModel.skill_key)
        )
        return [self._entity(row) for row in rows]

    async def get_for_user(self, user_id: str, skill_key: str) -> BuiltinSkillState | None:
        row = await self.session.scalar(
            select(UserBuiltinSkillStateModel).where(
                UserBuiltinSkillStateModel.user_id == user_id,
                UserBuiltinSkillStateModel.skill_key == skill_key,
            )
        )
        return None if row is None else self._entity(row)

    async def upsert(self, user_id: str, skill_key: str, enabled: bool) -> BuiltinSkillState:
        # created_at 显式写值：双方言 insert 绕过 Python 侧默认值，NOT NULL 列必须带值；
        # 冲突更新只改 enabled，保留原 created_at。
        values = {"id": new_id(), "user_id": user_id, "skill_key": skill_key, "enabled": enabled, "created_at": datetime.now(UTC)}
        bind = self.session.bind
        if bind is not None and bind.dialect.name == "postgresql":
            statement = pg_insert(UserBuiltinSkillStateModel).values(**values).on_conflict_do_update(
                constraint="uq_user_builtin_skill_states_user_key",
                set_={"enabled": enabled},
            )
            await self.session.execute(statement)
        elif bind is not None and bind.dialect.name == "sqlite":
            statement = sqlite_insert(UserBuiltinSkillStateModel).values(**values).on_conflict_do_update(
                index_elements=("user_id", "skill_key"),
                set_={"enabled": enabled},
            )
            await self.session.execute(statement)
        else:
            existing = await self.session.scalar(
                select(UserBuiltinSkillStateModel).where(
                    UserBuiltinSkillStateModel.user_id == user_id,
                    UserBuiltinSkillStateModel.skill_key == skill_key,
                )
            )
            if existing is None:
                self.session.add(UserBuiltinSkillStateModel(**values))
            else:
                existing.enabled = enabled
        await self.session.flush()
        state = await self.get_for_user(user_id, skill_key)
        if state is None:
            raise RuntimeError("builtin skill state upsert did not persist a record")
        return state

    @staticmethod
    def _entity(row: UserBuiltinSkillStateModel) -> BuiltinSkillState:
        return BuiltinSkillState(
            id=row.id,
            user_id=row.user_id,
            skill_key=row.skill_key,
            enabled=row.enabled,
            created_at=row.created_at,
        )
