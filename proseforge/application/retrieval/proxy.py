"""Unified retrieval proxy: every RAG scene-pack build goes through here.

Responsibilities:
1. Query distillation — the orchestrator slot model condenses the caller's
   goal/message into a short retrieval query (moved up from
   workflows/agent_executor.py so every caller shares it); any failure
   falls back to the raw query and must never break the caller.
2. Build — delegates to NarrativeRetriever.build and returns the ScenePack.

SECURITY: ``project_id`` MUST be resolved server-side from the message/run
context and passed in by the caller — never accept it from model output or
client input. Every retrieval leg (vector / keyword / story-bible) is
scoped by this project id, so a tainted id is a cross-project data leak.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from proseforge.application.work.retriever import NarrativeRetriever, ScenePack

if TYPE_CHECKING:
    from proseforge.domain.ports.model_provider import ModelProvider

logger = logging.getLogger(__name__)

# Narrative-RAG retrieval intent: the orchestrator slot distills the run
# goal into a short retrieval query; any failure falls back to the raw goal.
RAG_QUERY_INTENT_TIMEOUT_SECONDS = 10.0
RAG_QUERY_INTENT_MAX_CHARS = 200
RAG_QUERY_INTENT_PROMPT = (
    "你是检索意图分析 Agent。把用户的写作目标浓缩成一条章节检索查询：提取关键场景、人物、时间与设定要素，"
    "不超过 200 字。只输出查询文本本身，不要输出任何解释或格式。"
)


async def resolve_rag_query(provider: ModelProvider | None, model: str, goal: str, *, timeout: float = RAG_QUERY_INTENT_TIMEOUT_SECONDS) -> tuple[str, dict[str, object]]:
    """Orchestrator-slot retrieval intent for the scene-pack query.

    Returns ``(query, audit_payload)``: the model-distilled query (capped at
    RAG_QUERY_INTENT_MAX_CHARS) on success, or the raw run goal on any
    failure/timeout — an intent failure must never break the run. The model
    call happens outside any database transaction (same discipline as the
    executor's role handlers).
    """
    import asyncio

    from proseforge.domain.ports.model_provider import GenerationRequest

    if provider is None:
        return goal, {"source": "goal_fallback", "reason": "provider-unavailable"}
    if not goal.strip():
        return goal, {"source": "goal_fallback", "reason": "empty-goal"}
    request = GenerationRequest(
        model=model,
        system_blocks=({"role": "system", "text": RAG_QUERY_INTENT_PROMPT},),
        input_blocks=({"role": "user", "text": goal},),
        max_output_tokens=160,
        metadata={"workflow": "agent-run", "purpose": "rag-query-intent"},
    )
    parts: list[str] = []

    async def _collect() -> None:
        async for event in provider.stream(request):
            if event.event == "content.delta":
                parts.append(event.text)

    try:
        await asyncio.wait_for(_collect(), timeout=timeout)
    except Exception:
        logger.warning("rag query intent failed; falling back to run goal", exc_info=True)
        return goal, {"source": "goal_fallback", "reason": "model-error"}
    query = " ".join("".join(parts).split())[:RAG_QUERY_INTENT_MAX_CHARS]
    if not query:
        return goal, {"source": "goal_fallback", "reason": "empty-intent"}
    return query, {"source": "orchestrator", "model": model, "query": query}


async def retrieve_for_context(
    uow,
    *,
    project_id: str,
    query: str,
    orchestrator_ref: tuple[ModelProvider | None, str] | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    on_query: Callable[[str, dict[str, object]], Awaitable[None]] | None = None,
) -> ScenePack | None:
    """Distill ``query`` (orchestrator slot when given) and build the scene pack.

    ``project_id`` must be server-resolved from the message/run context (see
    module docstring); the retrieval user is the project owner, looked up
    here rather than trusted from the caller. ``orchestrator_ref`` is the
    (provider, model) slot used for distillation — None means no slot model,
    so the raw query is used unchanged. ``on_query`` (optional) runs after
    distillation and before the build, letting callers persist the
    rag.query_intent audit payload. Returns None only when the project does
    not exist; retriever failures propagate to the caller's degradation path.
    """
    from sqlalchemy import select

    from proseforge.infrastructure.database.models.project import ProjectModel
    from proseforge.settings import get_settings

    owner_id = await uow.session.scalar(select(ProjectModel.owner_id).where(ProjectModel.id == project_id))
    if not owner_id:
        return None
    provider, model = orchestrator_ref if orchestrator_ref is not None else (None, "")
    distilled, intent_event = await resolve_rag_query(provider, model, query)
    if on_query is not None:
        await on_query(distilled, intent_event)
    retriever = NarrativeRetriever(uow.session_factory, master_key=get_settings().master_key.get_secret_value())
    return await retriever.build(
        project_id=project_id, user_id=str(owner_id), query=distilled,
        conversation_id=conversation_id, message_id=message_id,
    )
