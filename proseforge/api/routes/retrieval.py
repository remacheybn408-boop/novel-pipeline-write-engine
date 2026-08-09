"""Retrieval snapshot (citation) endpoint for generated messages.

Returns the retrieval_runs snapshot recorded when the message's scene
pack was built: query, intent, hit chunks (chapter/document_title/score/expanded),
trimmed items with reasons, budgets, elapsed_ms and token_cost. A
message without a snapshot (switch off, chat mode, degraded build) gets
a uniform 404 — the snapshot is a sub-resource of the message, and
missing sub-resources are 404 elsewhere in this API.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.infrastructure.database.models.conversation import ConversationModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.retrieval import (
    RetrievalChunkModel,
    RetrievalDocumentModel,
    RetrievalRunModel,
)
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["retrieval"])


@router.get("/conversations/{conversation_id}/messages/{message_id}/retrieval")
async def get_message_retrieval(
    conversation_id: str,
    message_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        owner = await uow.session.scalar(
            select(ProjectModel.owner_id)
            .select_from(ConversationModel)
            .join(ProjectModel, ConversationModel.project_id == ProjectModel.id)
            .where(ConversationModel.id == conversation_id)
        )
        if owner != user.id:
            raise HTTPException(status_code=404, detail="conversation not found")
        run = await uow.session.scalar(
            select(RetrievalRunModel)
            .where(
                RetrievalRunModel.conversation_id == conversation_id,
                RetrievalRunModel.message_id == message_id,
            )
            .order_by(RetrievalRunModel.created_at.desc())
            .limit(1)
        )
        if run is None:
            raise HTTPException(status_code=404, detail="retrieval snapshot not found")
        payload = json.loads(run.selected_chunks_json or "{}")
        if isinstance(payload, list):
            # Legacy shape (pre-trim-recording): bare chunk list.
            payload = {"chunks": payload, "trimmed": [], "budget": {}}
        chunk_ids = [item["chunk_id"] for item in payload.get("chunks", []) if item.get("chunk_id")]
        titles: dict[str, str] = {}
        if chunk_ids:
            rows = await uow.session.execute(
                select(RetrievalChunkModel.id, RetrievalDocumentModel.title)
                .join(RetrievalDocumentModel, RetrievalChunkModel.document_id == RetrievalDocumentModel.id)
                .where(RetrievalChunkModel.id.in_(chunk_ids))
            )
            titles = {row.id: row.title for row in rows.all()}
        chunks = [
            {
                "chunk_id": item.get("chunk_id"),
                "chapter_no": item.get("chapter_no"),
                "document_title": titles.get(item.get("chunk_id"), ""),
                "score": item.get("score"),
                "expanded": bool(item.get("expanded")),
            }
            for item in payload.get("chunks", [])
        ]
        return {
            "run_id": run.id,
            "query_text": run.query_text,
            "intent": run.intent,
            "chunks": chunks,
            "trimmed": payload.get("trimmed", []),
            "budget": payload.get("budget", {}),
            "elapsed_ms": run.elapsed_ms,
            "token_cost": run.token_cost,
        }
