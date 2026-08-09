"""Scene-state endpoint (work-mode projects only)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.work.scene_state import build_scene_state
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["work"])


@router.get("/projects/{project_id}/scene-state")
async def get_scene_state(
    project_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        return await build_scene_state(uow, project_id, user.id)
