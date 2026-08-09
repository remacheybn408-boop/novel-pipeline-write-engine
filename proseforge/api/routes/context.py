from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.context.build_snapshot import BuildContextSnapshot
from proseforge.application.models.context_window import (
    catalog_model_snapshot,
    default_catalog_snapshot,
    resolve_context_window,
)
from proseforge.context_engine.budgeting import calculate_budget
from proseforge.context_engine.tokenizer import ConservativeTokenizer
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["context"])
preview_router = APIRouter(prefix="/api/v2", tags=["context"])


class ContextCreateRequest(BaseModel):
    source_type: str = Field(default="manual", min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=100000)
    source_id: str = "manual"


class ContextUpdateRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=100000)
    pinned: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    excluded: bool | None = None


class ContextPreviewRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100000)
    provider: str | None = None
    model: str | None = None


def _response(item) -> dict[str, object]:
    return {
        "id": item.id, "project_id": item.project_id, "source_type": item.source_type,
        "source_id": item.source_id, "content": item.content, "pinned": item.pinned,
        "priority": item.priority, "excluded": item.excluded, "provenance": json.loads(item.provenance or "{}"),
    }


def _snapshot_response(snapshot) -> dict[str, object]:
    return {
        "id": snapshot.id,
        "project_id": snapshot.project_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "payload": json.loads(snapshot.payload),
    }


@router.get("/projects/{project_id}/context")
async def list_context(project_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)], provider: str | None = None, model: str | None = None) -> dict[str, object]:
    if (provider is None) != (model is None):
        raise HTTPException(status_code=422, detail="provider and model must be provided together")
    async with uow:
        await require_work_project(uow, user.id, project_id)
        items = await uow.context.list_owned(project_id, user.id)
        tokenizer = ConservativeTokenizer()
        used_tokens = sum(tokenizer.count(item.content) for item in items if not item.excluded)
        # context window 来自 catalog：请求指定模型 → 该模型的窗口；未指定 →
        # catalog 默认（可用条目最小窗口）；都没有 → 保守 floor，source 显式记录。
        snapshot = await catalog_model_snapshot(uow, provider, model) if provider and model else await default_catalog_snapshot(uow)
        window = resolve_context_window(snapshot)
        context_window = int(window["context_window"])
        return {"items": [_response(item) for item in items], "used_tokens": used_tokens, "context_window": context_window, "context_window_source": window["source"], "available_tokens": max(0, context_window - used_tokens)}


@router.post("/projects/{project_id}/context/items", status_code=status.HTTP_201_CREATED)
async def add_context(project_id: str, payload: ContextCreateRequest, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        item = await uow.context.add(project_id, payload.source_type, payload.content, payload.source_id)
        await uow.commit()
        return _response(item)


@router.patch("/context/items/{item_id}")
async def update_context(item_id: str, payload: ContextUpdateRequest, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        item = await uow.context.get_owned(item_id, user.id)
        if item is None:
            raise HTTPException(status_code=404, detail="context item not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(item, key, value)
        await uow.commit()
        return _response(item)


@router.delete("/context/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_context(item_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> None:
    async with uow:
        item = await uow.context.get_owned(item_id, user.id)
        if item is None:
            raise HTTPException(status_code=404, detail="context item not found")
        await uow.session.delete(item)
        await uow.commit()


@router.post("/projects/{project_id}/context/compile")
async def compile_context(project_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        items = await uow.context.list_owned(project_id, user.id)
        snapshot = await uow.context.snapshot(project_id, items)
        await uow.commit()
        return {"id": snapshot.id, "snapshot_hash": snapshot.snapshot_hash, "item_count": len(items)}


@router.get("/context/snapshots/{snapshot_id}")
async def get_context_snapshot(snapshot_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        snapshot = await uow.context.get_snapshot_owned(snapshot_id, user.id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="context snapshot not found")
        return _snapshot_response(snapshot)


@router.post("/context/snapshots/{snapshot_id}/validate")
async def validate_context_snapshot(snapshot_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        snapshot = await uow.context.get_snapshot_owned(snapshot_id, user.id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="context snapshot not found")
        encoded = json.dumps(json.loads(snapshot.payload), ensure_ascii=False, sort_keys=True)
        actual_hash = hashlib.sha256(encoded.encode()).hexdigest()
        return {"id": snapshot.id, "valid": actual_hash == snapshot.snapshot_hash, "snapshot_hash": snapshot.snapshot_hash, "actual_hash": actual_hash}


@router.get("/context/snapshots/{snapshot_id}/download")
async def download_context_snapshot(snapshot_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> Response:
    async with uow:
        snapshot = await uow.context.get_snapshot_owned(snapshot_id, user.id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="context snapshot not found")
        body = json.dumps(_snapshot_response(snapshot), ensure_ascii=False, indent=2)
    return Response(content=body, media_type="application/json", headers={"content-disposition": f'attachment; filename="context-{snapshot_id}.json"'})


@preview_router.post("/projects/{project_id}/context/preview")
async def preview_context(project_id: str, payload: ContextPreviewRequest, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    if (payload.provider is None) != (payload.model is None):
        raise HTTPException(status_code=422, detail="provider and model must be provided together")
    async with uow:
        await require_work_project(uow, user.id, project_id)
        model_snapshot = await catalog_model_snapshot(uow, payload.provider, payload.model) if payload.provider and payload.model else await default_catalog_snapshot(uow)
        window = resolve_context_window(model_snapshot)
        context_window = int(window["context_window"])
        output_reserve = int((model_snapshot or {}).get("max_output_tokens") or 1024)
        budget = calculate_budget(context_window, output_reserve)
        rows = (await uow.session.scalars(select(StoryBibleEntryModel).where(StoryBibleEntryModel.project_id == project_id).order_by(StoryBibleEntryModel.kind, StoryBibleEntryModel.key))).all()
        builder = BuildContextSnapshot()
        selection = builder.select_story_facts(rows, payload.text, budget.input_tokens)
        blocks = [builder.describe_block(block_type="persona", source_id="default", text="Story Bible preview", priority=100), *selection.blocks]
        response_payload = {
            "blocks": blocks,
            "omitted": list(selection.omitted),
            "injected_fact_ids": list(selection.injected_fact_ids),
            "injected_fact_reasons": {str(block["source_id"]): str(block.get("reason", "")) for block in selection.blocks},
            "budget": {"context_window": budget.context_window, "input_tokens": budget.input_tokens, "output_reserve": budget.output_reserve},
        }
        encoded = json.dumps(response_payload, ensure_ascii=False, sort_keys=True)
        return {"id": "preview", "project_id": project_id, "snapshot_hash": hashlib.sha256(encoded.encode()).hexdigest(), "payload": response_payload}
