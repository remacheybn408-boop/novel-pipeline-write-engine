from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.infrastructure.database.models.tool_call import ToolCallLogModel


@dataclass(frozen=True)
class ToolCallRecord:
    call_id: str
    message_id: str
    conversation_id: str
    user_id: str
    tool_name: str
    status: str
    error_class: str | None
    params_json: str
    result_summary: str
    result_bytes: int
    cache_hit: bool
    attempt: int
    duration_ms: float
    started_at: datetime
    finished_at: datetime | None
    resource_json: str
    created_at: datetime = field(default=None)


class SqlAlchemyToolCallRepository:
    """Append-mostly audit log for chat tool calls."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, **fields) -> ToolCallRecord:
        row = ToolCallLogModel(**fields)
        self.session.add(row)
        await self.session.flush()
        return self._entity(row)

    async def get(self, call_id: str) -> ToolCallRecord | None:
        row = await self.session.get(ToolCallLogModel, call_id)
        return self._entity(row) if row else None

    async def list_for_conversation(self, conversation_id: str, limit: int = 200) -> list[ToolCallRecord]:
        rows = await self.session.scalars(
            select(ToolCallLogModel)
            .where(ToolCallLogModel.conversation_id == conversation_id)
            .order_by(ToolCallLogModel.created_at)
            .limit(limit)
        )
        return [self._entity(row) for row in rows]

    @staticmethod
    def _entity(row: ToolCallLogModel) -> ToolCallRecord:
        return ToolCallRecord(
            call_id=row.call_id,
            message_id=row.message_id,
            conversation_id=row.conversation_id,
            user_id=row.user_id,
            tool_name=row.tool_name,
            status=row.status,
            error_class=row.error_class,
            params_json=row.params_json,
            result_summary=row.result_summary,
            result_bytes=row.result_bytes,
            cache_hit=row.cache_hit,
            attempt=row.attempt,
            duration_ms=row.duration_ms,
            started_at=row.started_at,
            finished_at=row.finished_at,
            resource_json=row.resource_json,
            created_at=row.created_at,
        )
