from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.domain.usage import UsageDelta
from proseforge.infrastructure.database.models.remaining import WorkflowRunModel
from proseforge.infrastructure.database.models.usage import ModelUsageRecordModel


class SqlAlchemyUsageRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(self, *, user_id: str, provider: str, model_id: str, call_id: str, delta: UsageDelta, project_id: str | None = None, conversation_id: str | None = None, message_id: str | None = None, workflow_run_id: str | None = None, workflow_step: str | None = None, provider_request_id: str | None = None, cost_usd: float | None = None, latency_ms: float | None = None, metadata: dict[str, object] | None = None) -> ModelUsageRecordModel:
        values = {
            "id": new_id(),
            "user_id": user_id,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "workflow_run_id": workflow_run_id,
            "workflow_step": workflow_step,
            "provider": provider,
            "model_id": model_id,
            "provider_request_id": provider_request_id or delta.provider_request_id,
            "call_id": call_id,
            "input_tokens": delta.input_tokens,
            "output_tokens": delta.output_tokens,
            "cached_input_tokens": delta.cached_input_tokens,
            "reasoning_tokens": delta.reasoning_tokens,
            "total_tokens": max(delta.total_tokens, delta.input_tokens + delta.output_tokens),
            "accounted_total_tokens": 0,
            "cost_usd": cost_usd,
            "usage_source": delta.source,
            "is_final": delta.final,
            "latency_ms": latency_ms,
            "created_at": datetime.now(UTC),
            "metadata_json": json.dumps(metadata if metadata is not None else delta.raw_metadata, ensure_ascii=False),
        }
        bind = self.session.bind
        if bind is None:
            raise RuntimeError("usage repository is not bound to a database")
        insert = pg_insert(ModelUsageRecordModel) if bind.dialect.name == "postgresql" else sqlite_insert(ModelUsageRecordModel)
        statement = insert.values(**values)
        excluded = statement.excluded
        current = ModelUsageRecordModel
        merged_input = case((excluded.input_tokens > current.input_tokens, excluded.input_tokens), else_=current.input_tokens)
        merged_output = case((excluded.output_tokens > current.output_tokens, excluded.output_tokens), else_=current.output_tokens)
        merged_cached = case((excluded.cached_input_tokens > current.cached_input_tokens, excluded.cached_input_tokens), else_=current.cached_input_tokens)
        merged_reasoning = case((excluded.reasoning_tokens > current.reasoning_tokens, excluded.reasoning_tokens), else_=current.reasoning_tokens)
        provider_total = case((excluded.total_tokens > current.total_tokens, excluded.total_tokens), else_=current.total_tokens)
        merged_total = case((provider_total >= merged_input + merged_output, provider_total), else_=merged_input + merged_output)
        statement = statement.on_conflict_do_update(
            index_elements=("call_id",),
            set_={
                "project_id": excluded.project_id,
                "conversation_id": excluded.conversation_id,
                "message_id": excluded.message_id,
                "workflow_run_id": excluded.workflow_run_id,
                "workflow_step": excluded.workflow_step,
                "provider_request_id": func.coalesce(excluded.provider_request_id, current.provider_request_id),
                "input_tokens": merged_input,
                "output_tokens": merged_output,
                "cached_input_tokens": merged_cached,
                "reasoning_tokens": merged_reasoning,
                "total_tokens": merged_total,
                "cost_usd": func.coalesce(excluded.cost_usd, current.cost_usd),
                "usage_source": excluded.usage_source,
                "is_final": or_(current.is_final, excluded.is_final),
                "latency_ms": func.coalesce(excluded.latency_ms, current.latency_ms),
                "metadata": excluded["metadata"],
            },
        )
        await self.session.execute(statement)
        row = await self.session.scalar(
            select(ModelUsageRecordModel)
            .where(ModelUsageRecordModel.call_id == call_id)
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("usage upsert did not persist a record")
        if workflow_run_id:
            workflow = await self.session.scalar(
                select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_run_id).with_for_update()
            )
            if workflow is not None:
                increase = max(0, int(row.total_tokens or 0) - int(row.accounted_total_tokens or 0))
                workflow.used_tokens = max(0, int(workflow.used_tokens or 0) + increase)
                row.accounted_total_tokens = int(row.total_tokens or 0)
        else:
            row.accounted_total_tokens = int(row.total_tokens or 0)
        await self.session.flush()
        return row

    async def list_for_user(self, user_id: str, *, project_id: str | None = None, conversation_id: str | None = None, workflow_run_id: str | None = None, message_id: str | None = None, limit: int = 100) -> list[ModelUsageRecordModel]:
        query = select(ModelUsageRecordModel).where(ModelUsageRecordModel.user_id == user_id).order_by(ModelUsageRecordModel.created_at.desc()).limit(max(1, min(limit, 500)))
        if project_id:
            query = query.where(ModelUsageRecordModel.project_id == project_id)
        if conversation_id:
            query = query.where(ModelUsageRecordModel.conversation_id == conversation_id)
        if workflow_run_id:
            query = query.where(ModelUsageRecordModel.workflow_run_id == workflow_run_id)
        if message_id:
            query = query.where(ModelUsageRecordModel.message_id == message_id)
        return list(await self.session.scalars(query))

    async def aggregate_by_model(self, user_id: str, *, since: datetime) -> list[dict[str, object]]:
        """GROUP BY provider+model_id over the user's records created since `since`."""
        query = (
            select(
                ModelUsageRecordModel.provider,
                ModelUsageRecordModel.model_id,
                func.count().label("calls"),
                func.coalesce(func.sum(ModelUsageRecordModel.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(ModelUsageRecordModel.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(ModelUsageRecordModel.cached_input_tokens), 0).label("cached_input_tokens"),
                func.coalesce(func.sum(ModelUsageRecordModel.reasoning_tokens), 0).label("reasoning_tokens"),
                func.coalesce(func.sum(ModelUsageRecordModel.total_tokens), 0).label("total_tokens"),
                func.sum(ModelUsageRecordModel.cost_usd).label("cost_usd"),
                func.avg(ModelUsageRecordModel.latency_ms).label("avg_latency_ms"),
                func.max(ModelUsageRecordModel.created_at).label("last_used_at"),
            )
            .where(ModelUsageRecordModel.user_id == user_id, ModelUsageRecordModel.created_at >= since)
            .group_by(ModelUsageRecordModel.provider, ModelUsageRecordModel.model_id)
        )
        result = await self.session.execute(query)
        return [dict(row._mapping) for row in result]

    async def list_all_for_user(self, user_id: str, *, project_id: str | None = None, conversation_id: str | None = None, workflow_run_id: str | None = None, message_id: str | None = None) -> list[ModelUsageRecordModel]:
        query = select(ModelUsageRecordModel).where(ModelUsageRecordModel.user_id == user_id).order_by(ModelUsageRecordModel.created_at.desc())
        if project_id:
            query = query.where(ModelUsageRecordModel.project_id == project_id)
        if conversation_id:
            query = query.where(ModelUsageRecordModel.conversation_id == conversation_id)
        if workflow_run_id:
            query = query.where(ModelUsageRecordModel.workflow_run_id == workflow_run_id)
        if message_id:
            query = query.where(ModelUsageRecordModel.message_id == message_id)
        return list(await self.session.scalars(query))
