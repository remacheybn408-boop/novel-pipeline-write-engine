"""Tool metrics endpoint: per-user SQL aggregation over tool_call_log."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.tools.metrics import build_tool_metrics, resolve_window
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])


@router.get("/metrics")
async def get_tool_metrics(
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    days: int = Query(default=7),
) -> dict:
    """Aggregate the CURRENT user's tool calls only. days: 1/7/30 (else 7)."""
    async with uow:
        return await build_tool_metrics(uow.session, user_id=user.id, days=resolve_window(days))
