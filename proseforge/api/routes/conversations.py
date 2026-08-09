from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.api.routes.branches import (
    _resolve_reasoning_level,
    _resolve_target_model,
    _validate_reasoning_level,
)
from proseforge.api.sse.encoder import encode_sse
from proseforge.application.auth.service import AuthUser
from proseforge.application.models.cluster_config import available_model_refs
from proseforge.application.models.resolve_model import FALLBACK_CAPABILITIES
from proseforge.domain.model.capabilities import capabilities_from_model
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["conversations"])

SSE_HEARTBEAT_SECONDS = 15.0


class ConversationCreateRequest(BaseModel):
    project_id: str
    title: str = "Untitled conversation"


class MessageRequest(BaseModel):
    branch_id: str
    content: str = Field(min_length=1)
    client_request_id: str = Field(min_length=1, max_length=128)
    # Pre-uploaded file ids (POST /projects/{id}/files): validated against
    # ownership + this conversation's project below, linked to the persisted
    # user message, and injected into the model context at generation time.
    attachment_ids: list[str] = []
    # 缺省（None）= 用户未指定：回落默认 openai/gpt-4.1-mini；与显式指定
    # 区分是为了能在"未指定且系统无任何可用模型"时路由层 422，而不是静默
    # 回落一个注定失败的模型。
    provider: str | None = None
    model: str | None = None
    reasoning_level: str = "auto"  # 入队前按目标模型 catalog 校验（与 v2 同规则）
    mode: Literal["swarm"] | None = None  # swarm: 意图路由（闲聊→write 模型回复；写作/审校/改写→agent run）


class BranchRequest(BaseModel):
    message_id: str
    name: str = Field(min_length=1, max_length=200)


class MessageControlRequest(BaseModel):
    provider: str | None = None  # 缺省 → 复用消息落库 model_snapshot
    model: str | None = None
    reasoning_level: str | None = None


@router.post("/conversations")
async def create_conversation(
    payload: ConversationCreateRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, str]:
    async with uow:
        # The project repository lookup is the ownership boundary.
        project = await uow.projects.get_by_id(user.id, payload.project_id)
        if project is None:
            project = await uow.projects.get_by_slug(user.id, payload.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        from proseforge.domain.conversation.entity import Conversation
        conversation = Conversation.create(project.id, payload.title)
        branch = await uow.conversations.create(conversation)
        await uow.commit()
        return {"id": conversation.id, "branch_id": branch.id, "title": conversation.title}


class ConversationPatchRequest(BaseModel):
    archived: bool


@router.get("/conversations")
async def list_conversations(
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    mode: Literal["work", "chat"] | None = None,
    project_id: str | None = None,
    archived: bool = False,
) -> list[dict[str, object]]:
    async with uow:
        conversations = await uow.conversations.list_for_owner(user.id, mode=mode, project_id=project_id, archived=archived)
        return [
            {"id": item.id, "project_id": item.project_id, "title": item.title, "created_at": item.created_at.isoformat(), "archived": item.archived}
            for item in conversations
        ]


@router.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    payload: ConversationPatchRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        if not await uow.conversations.set_archived(conversation_id, user.id, payload.archived):
            raise HTTPException(status_code=404, detail="conversation not found")
        await uow.commit()
        return {"id": conversation_id, "archived": payload.archived}


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> None:
    async with uow:
        if not await uow.conversations.delete_owned(conversation_id, user.id):
            raise HTTPException(status_code=404, detail="conversation not found")
        await uow.commit()


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    payload: MessageRequest,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
) -> dict[str, object]:  # object: swarm path adds intent/agent_run_id (nullable)
    async with unit_of_work(request) as uow:
        if not await uow.conversations.branch_belongs_to_conversation(payload.branch_id, conversation_id, user.id):
            raise HTTPException(status_code=404, detail="conversation or branch not found")
        provider = payload.provider or "openai"
        model = payload.model or "gpt-4.1-mini"
        if payload.model is None and not await available_model_refs(uow, user.id):
            # 未指定模型时本会静默回落 openai/gpt-4.1-mini；但用户连一个可用
            # 模型都没有（无凭证/目录为空），入队注定失败——提前 422 并指明
            # 去设置页配置凭证。显式指定模型的请求不做此检查，行为不变。
            raise HTTPException(status_code=422, detail="当前没有任何可用模型：请先到设置页配置模型凭证，或在发送时显式指定模型")
        # 与 v2 同规则：入队前按目标模型 catalog 校验，不支持 → 422（此前静默
        # 入队，worker 吞成 {"supported": False}，用户无感知）。swarm 与默认
        # 路径共用本次校验。
        catalog = await uow.model_catalog.get(provider, model)
        capabilities = capabilities_from_model(catalog) if catalog is not None else FALLBACK_CAPABILITIES
        _validate_reasoning_level(payload.reasoning_level, capabilities)
        # Unified dispatch needs the project mode for every message (swarm
        # routing and normal-mode collapsed runs both key off it).
        from sqlalchemy import select as _select

        from proseforge.infrastructure.database.models.conversation import (
            ConversationModel as _ConversationModel,
        )
        from proseforge.infrastructure.database.models.project import (
            ProjectModel as _ProjectModel,
        )

        project_mode = await uow.session.scalar(
            _select(_ProjectModel.mode)
            .join(_ConversationModel, _ConversationModel.project_id == _ProjectModel.id)
            .where(_ConversationModel.id == conversation_id)
        )
        # Dedupe preserving order, then verify every attachment is owned by
        # the caller AND belongs to this conversation's project — anything
        # else is a client bug or an IDOR probe, rejected as 400.
        attachment_ids = list(dict.fromkeys(payload.attachment_ids))
        if attachment_ids:
            project_id = await uow.session.scalar(
                _select(_ProjectModel.id)
                .join(_ConversationModel, _ConversationModel.project_id == _ProjectModel.id)
                .where(_ConversationModel.id == conversation_id)
            )
            for attachment_id in attachment_ids:
                attachment = await uow.attachments.get_owned(attachment_id, user.id)
                if attachment is None or attachment.project_id != project_id:
                    raise HTTPException(status_code=400, detail="invalid attachment id")
    if payload.mode == "swarm" and project_mode == "work":
        from proseforge.application.agents.swarm_entry import handle_swarm_message
        return await handle_swarm_message(
            lambda: unit_of_work(request), request.app.state.queue,
            master_key=request.app.state.settings.master_key,
            environment=request.app.state.settings.environment,
            branch_id=payload.branch_id, content=payload.content,
            client_request_id=payload.client_request_id, user_id=user.id,
            provider=provider, model=model,
            reasoning_level=payload.reasoning_level,
            attachment_ids=tuple(attachment_ids),
            # Fake-settings test doubles may lack blob_root; "" falls back to
            # get_settings() inside run_entry_response.
            blob_root=getattr(request.app.state.settings, "blob_root", ""),
            settings=request.app.state.settings,
        )
    from proseforge.application.dispatch import dispatch_normal_message

    return await dispatch_normal_message(
        lambda: unit_of_work(request), request.app.state.queue,
        master_key=request.app.state.settings.master_key,
        environment=request.app.state.settings.environment,
        branch_id=payload.branch_id, content=payload.content,
        client_request_id=payload.client_request_id, user_id=user.id,
        provider=provider, model=model,
        reasoning_level=payload.reasoning_level,
        project_mode=project_mode,
        attachment_ids=tuple(attachment_ids),
        blob_root=getattr(request.app.state.settings, "blob_root", ""),
    )


@router.get("/conversations/{conversation_id}/branches/{branch_id}/messages")
async def list_messages(
    conversation_id: str,
    branch_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> list[dict[str, object]]:
    async with uow:
        if not await uow.conversations.branch_belongs_to_conversation(branch_id, conversation_id, user.id):
            raise HTTPException(status_code=404, detail="conversation or branch not found")
        messages = await uow.conversations.list_visible_messages(branch_id)
        from proseforge.application.files.message_attachments import (
            attachments_for_messages,
        )

        # User-uploaded attachments linked to each message (filename chips +
        # download links in the user bubble; empty list for most messages).
        attachment_map = await attachments_for_messages(uow.session, [item.id for item in messages])
        return [{
            "id": item.id, "role": item.role, "content": item.content, "status": item.status,
            "context_snapshot_id": (item.model_snapshot or {}).get("context_snapshot_id"),
            "agent_run_id": item.agent_run_id,
            # Regenerate grouping metadata: lets the frontend collapse sibling
            # candidates of one parent edge into a single bubble group.
            "generation_attempt": item.generation_attempt,
            "parent_message_id": item.parent_message_id,
            "attachments": [{"id": row.id, "filename": row.filename} for row in attachment_map.get(item.id, [])],
        } for item in messages]


@router.post("/conversations/{conversation_id}/branches")
async def fork_branch(
    conversation_id: str,
    payload: BranchRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, str]:
    async with uow:
        branch = await uow.conversations.fork_owned(conversation_id, payload.message_id, payload.name, user.id)
        if branch is None:
            raise HTTPException(status_code=404, detail="conversation or fork point not found")
        await uow.commit()
        return {"id": branch.id, "name": branch.name}


async def _owned_message(message_id: str, user: AuthUser, request: Request):
    async with unit_of_work(request) as uow:
        conversation_id = await uow.conversations.conversation_id_for_message(message_id)
        if conversation_id is None or not await uow.conversations.belongs_to_owner(conversation_id, user.id):
            raise HTTPException(status_code=404, detail="message not found")
        message = await uow.conversations.get_message(message_id)
        if message is None:
            raise HTTPException(status_code=404, detail="message not found")
        return message


@router.post("/messages/{message_id}/stop")
async def stop_message(message_id: str, request: Request, user: Annotated[AuthUser, Depends(current_user)]) -> dict[str, str]:
    message = await _owned_message(message_id, user, request)
    if message.agent_run_id:
        # Swarm placeholder messages are owned by the agent run: flipping the
        # message status here does not stop the running swarm. Same guard as
        # _requeue_message — point the caller at the run controls instead.
        raise HTTPException(status_code=409, detail="message is owned by an agent run; cancel it via the agent-run controls")
    if message.status not in {"PENDING", "STREAMING", "PARTIAL"}:
        raise HTTPException(status_code=409, detail="message cannot be stopped in its current state")
    async with unit_of_work(request) as uow:
        # 条件 UPDATE：上面的状态预检与本次写入之间 worker 可能已 COMPLETED，
        # 无条件覆盖会把已完成消息永久翻成 CANCELLED（TOCTOU）。影响行数为 0
        # 即竞态落败 → 409，不重试、不覆盖。
        if not await uow.conversations.cancel_message_if_active(message_id, {"PENDING", "STREAMING", "PARTIAL"}):
            raise HTTPException(status_code=409, detail="message cannot be stopped in its current state")
        conversation_id = await uow.conversations.conversation_id_for_message(message_id)
        await uow.commit()
    # 取消成功即发布终止事件：worker 侧的 GenerateReply 看到 CANCELLED 会静默
    # return（不发布任何终态），SSE live tail 没有 message.failed 会永远挂起。
    event_stream = getattr(request.app.state, "event_stream", None)
    if event_stream is not None:
        payload = {"event": "message.failed", "message_id": message_id, "status": "CANCELLED", "reason": "cancelled-by-user"}
        await event_stream.publish(f"message:{message_id}", payload)
        if conversation_id:
            await event_stream.publish(f"conversation:{conversation_id}", payload)
    return {"id": message_id, "status": "CANCELLED"}


async def _requeue_message(message_id: str, payload: MessageControlRequest, request: Request, user: AuthUser, allowed: set[str]) -> dict[str, str]:
    message = await _owned_message(message_id, user, request)
    if message.agent_run_id:
        # Swarm 占位消息由 agent run 拥有：按普通单模型消息 requeue 会把集群
        # 结果降级成普通回复。引导去 run 详情页用 agent-run 的 retry/resume。
        raise HTTPException(status_code=409, detail="message is owned by an agent run; use the agent-run controls on the run detail page")
    if message.status not in allowed:
        # 注：CANCELLED 不在任何 allowed 集合里，取消后永久不可重试——是否为
        # 有意设计存疑（审计 L9），保持现状仅作记录。
        raise HTTPException(status_code=409, detail="message is not recoverable in its current state")
    provider, model = _resolve_target_model(payload, message)
    # PARTIAL 保持原状态入队：worker（generate_chat）按 status == "PARTIAL" 走
    # 续写门（历史剔除 partial + 追加 continue block）；翻成 PENDING 会重生成、
    # 旧内容与新流拼在一起导致重复。FAILED 才翻 PENDING 全量重试。
    resume = message.status == "PARTIAL"
    async with unit_of_work(request) as uow:
        if payload.reasoning_level:
            # 显式级别与 send 同规则：入队前按目标模型 catalog 校验，不支持 → 422。
            catalog = await uow.model_catalog.get(provider, model)
            capabilities = capabilities_from_model(catalog) if catalog is not None else FALLBACK_CAPABILITIES
            _validate_reasoning_level(payload.reasoning_level, capabilities)
        if not resume:
            await uow.conversations.set_message_status(message_id, "PENDING")
        await uow.commit()
    reasoning_level = _resolve_reasoning_level(payload.reasoning_level, message.reasoning_snapshot)
    task_id = await request.app.state.queue.enqueue("proseforge.chat.generate", {"message_id": message_id, "user_id": user.id, "provider": provider, "model": model, "reasoning_level": reasoning_level})
    return {"id": message_id, "status": "PARTIAL" if resume else "PENDING", "task_id": task_id}


@router.post("/messages/{message_id}/retry")
async def retry_message(message_id: str, payload: MessageControlRequest, request: Request, user: Annotated[AuthUser, Depends(current_user)]) -> dict[str, str]:
    return await _requeue_message(message_id, payload, request, user, {"FAILED", "PARTIAL"})


@router.post("/messages/{message_id}/continue")
async def continue_message(message_id: str, payload: MessageControlRequest, request: Request, user: Annotated[AuthUser, Depends(current_user)]) -> dict[str, str]:
    return await _requeue_message(message_id, payload, request, user, {"PARTIAL"})


@router.get("/conversations/{conversation_id}/events")
async def stream_events(conversation_id: str, request: Request, user: Annotated[AuthUser, Depends(current_user)]):
    async with unit_of_work(request) as uow:
        if not await uow.conversations.belongs_to_owner(conversation_id, user.id):
            raise HTTPException(status_code=404, detail="conversation not found")
    last_id = request.headers.get("last-event-id")

    async def body():
        subscription = request.app.state.event_stream.subscribe(f"conversation:{conversation_id}", last_id)
        iterator = subscription.__aiter__()
        pending: asyncio.Task | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(iterator.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=SSE_HEARTBEAT_SECONDS)
                if not done:
                    yield b": heartbeat\n\n"
                    continue
                pending = None
                try:
                    event = done.pop().result()
                except StopAsyncIteration:
                    return
                yield encode_sse(event_id=str(event["id"]), event=str(event.get("event", "message")), data=event)
        finally:
            # 客户端断连时 StreamingResponse 取消本生成器；清理待取的轮询任务。
            if pending is not None:
                pending.cancel()
                # Await the cancelled task so its CancelledError is consumed
                # instead of surfacing as "Task exception was never retrieved".
                await asyncio.gather(pending, return_exceptions=True)
            await subscription.aclose()

    return StreamingResponse(body(), media_type="text/event-stream", headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
