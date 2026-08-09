"""任务 handler 实现（celery-free，V15-004）。

6 个持久任务的实际执行体，统一签名为
``async handler(payload: dict[str, object]) -> object``。
模块级不 import celery——native profile 的 LocalTaskQueue 直接复用
HANDLERS；celery_app.py 只做薄封装（asyncio.run + celery.task 注册）。
重型依赖全部在函数体内惰性 import。v2 workflow 执行器在 v2_tasks.py。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from proseforge.workflows.v2_tasks import execute_v2_run

TaskHandler = Callable[[dict[str, object]], Awaitable[object]]

# generate_chat scene-pack trim: tokens reserved for the chat compiler's
# own system blocks (persona + tool contract + outline + skills, the
# latter capped at SKILL_BLOCK_MAX_CHARS=8000 chars) so the trimmed pack
# plus those blocks stay inside the model input budget.
CHAT_SCENE_PACK_RESERVE_TOKENS = 4500


def should_abort_workflow(status: str) -> bool:
    return status == "CANCELLED"


async def generate_novel(payload: dict[str, object]) -> str:
    import asyncio
    import base64
    import json

    from proseforge.application.models.cluster_config import resolve_role_models
    from proseforge.application.models.context_window import (
        catalog_model_snapshot,
        resolve_context_window,
    )
    from proseforge.application.work.retriever import (
        NarrativeRetriever,
        narrative_rag_switch_enabled,
        trim_scene_pack,
    )
    from proseforge.application.workflows.control import decode_checkpoint
    from proseforge.context_engine.budgeting import calculate_budget
    from proseforge.context_engine.compiler import compile_context
    from proseforge.domain.common.ids import new_id
    from proseforge.domain.workflow.budget import budget_blocked
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.infrastructure.security.credential_cipher import (
        CredentialCipher,
        derive_key,
    )
    from proseforge.providers.factory import build_provider
    from proseforge.settings import get_settings
    from proseforge.workflows.novel_generation import run_writer_editor_loop

    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    workflow_id = str(payload["workflow_id"])
    owner_id = str(payload.get("user_id", ""))
    provider_id = str(payload.get("provider", "openai"))
    model_id = str(payload.get("model", "gpt-4.1-mini"))
    lease_owner = f"celery:{workflow_id}:{payload.get('task_id') or new_id()!s}"
    # celery 包装层注入的当前任务 id（control.set_task 把最近一次入队的
    # task_id 写进 checkpoint.active_task_id）；无该键（本地队列/直调）时
    # 接力检测自动跳过。
    my_task_id = str(payload.get("task_id", "") or "")

    def superseded(run_status: str, checkpoint: str | None) -> bool:
        # resume/retry 已入队继任任务，旧任务必须在章节边界尽快让位：
        # RETRYING 覆盖“继任尚未翻回 RUNNING”的窗口；active_task_id 覆盖
        # “继任已翻回 RUNNING”的窗口（此时状态上已看不出交接）。
        if run_status == "RETRYING":
            return True
        if my_task_id:
            active = decode_checkpoint(checkpoint).get("active_task_id")
            if isinstance(active, str) and active and active != my_task_id:
                return True
        return False

    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            run = await uow.workflows.get_owned(workflow_id, owner_id, lock=True)
            if run is None:
                return "workflow-not-found"
            if should_abort_workflow(run.status):
                return "cancelled"
            if run.status in {"QUEUED", "RETRYING"}:
                await uow.workflows.transition(run, "RUNNING")
            if not await uow.workflows.acquire_lease(run, lease_owner):
                return "lease-unavailable"
            project = await uow.projects.get_by_id(owner_id, run.project_id)
            chapters = await uow.chapters.list_owned(run.project_id, owner_id)
            if project is None:
                await uow.workflows.transition(run, "FAILED")
                await uow.commit()
                return "provider-or-project-not-configured"
            # Writing-model lock + cluster roles: once locked, the
            # requested model is ignored; reasoning level is untouched.
            # Cluster config honors the project override (project > global).
            locked_ref = (project.writing_model_provider, project.writing_model_id) if project.model_locked_at else None
            roles = await resolve_role_models(uow, owner_id, locked=locked_ref, requested=(provider_id, model_id), project_id=run.project_id)
            provider_id, model_id = roles.write
            role_providers: dict[str, object] = {}
            raw = derive_key(settings.master_key.get_secret_value())
            for role_ref in {roles.write, roles.review, roles.revise}:
                if role_ref[0] in role_providers:
                    continue
                role_credential = await uow.credentials.get_for_user(owner_id, role_ref[0])
                if role_credential is None:
                    continue
                role_associated = f"{owner_id}:{role_ref[0]}:{role_credential.id}".encode()
                role_secret = json.loads(CredentialCipher(raw).decrypt(base64.b64decode(role_credential.encrypted_payload), associated_data=role_associated))
                role_providers[role_ref[0]] = build_provider(role_ref[0], role_secret["api_key"], role_secret.get("base_url"))
            if roles.write[0] not in role_providers:
                await uow.workflows.transition(run, "FAILED")
                await uow.commit()
                return "provider-or-project-not-configured"
            provider = role_providers[roles.write[0]]
            requested = [int(item) for item in payload.get("chapter_numbers", [])]
            targets = [chapter for chapter in chapters if not requested or chapter.chapter_no in requested]
            if not targets:
                await uow.workflows.transition(run, "FAILED")
                await uow.commit()
                return "no-chapters"
            context_items = [item for item in await uow.context.list_owned(run.project_id, owner_id) if not item.excluded]
            context_blocks = [{"id": item.id, "source_type": item.source_type, "content": item.content, "pinned": item.pinned, "priority": item.priority} for item in context_items]
            snapshot = await uow.context.snapshot(run.project_id, context_items)
            model_snapshot = await catalog_model_snapshot(uow, provider_id, model_id)  # catalog 为事实
            window = resolve_context_window(model_snapshot)
            budget = calculate_budget(int(window["context_window"]), int(model_snapshot["max_output_tokens"]))
            compiled_context = compile_context(snapshot.id, context_blocks, input_budget=budget.input_tokens)
            context_text = "\n".join(str(block.get("content", "")) for block in compiled_context.blocks)
            rag_enabled = await narrative_rag_switch_enabled(uow, owner_id)
            await uow.workflows.append_event(workflow_id, "context.budget", {
                "context_window": window["context_window"],
                "context_window_source": window["source"],
                "input_budget": budget.input_tokens,
                "output_reserve": budget.output_reserve,
            })  # 窗口解析落痕：fallback 显式记录，绝不静默
            await uow.workflows.checkpoint(run, lease_owner, f"PREPARING_CONTEXT_{snapshot.snapshot_hash}")
            await uow.commit()

        for chapter in targets:
            usage_sequence = 0

            def usage_call_id(role: str) -> str:
                # B023 false positive: this callback (and record_usage below) is
                # passed to `await run_writer_editor_loop(...)` and fully consumed
                # within this same for-iteration; `chapter`/`run` are never
                # rebound before the callbacks run.
                nonlocal usage_sequence
                usage_sequence += 1
                return f"workflow:{workflow_id}:chapter:{chapter.id}:{role}:{usage_sequence}"  # noqa: B023 -- consumed in-iteration, see above

            async def record_usage(call_id: str, call_provider: str, call_model: str, role: str, delta) -> None:
                async with SqlAlchemyUnitOfWork(session_factory) as usage_uow:
                    await usage_uow.usage.record(
                        user_id=owner_id,
                        provider=call_provider,
                        model_id=call_model,
                        call_id=call_id,
                        delta=delta,
                        project_id=run.project_id,  # noqa: B023 -- consumed in-iteration, see above
                        workflow_run_id=workflow_id,
                        workflow_step=f"chapter:{chapter.chapter_no}:{role}",  # noqa: B023 -- consumed in-iteration, see above
                    )
                    await usage_uow.commit()
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                run = await uow.workflows.get_owned(workflow_id, owner_id)
                if run is None:
                    return "workflow-not-found"
                if should_abort_workflow(run.status):
                    return "cancelled"
                if run.status == "PAUSED":
                    return "paused"
                if superseded(run.status, run.checkpoint):
                    return "superseded"
                if run.lease_owner != lease_owner:
                    return "superseded"
                if budget_blocked(used_tokens=int(getattr(run, "used_tokens", 0) or 0), token_limit=int(getattr(run, "token_limit", 0) or 0), estimated_next_tokens=1, estimated_cost=float(run.estimated_cost or 0), cost_limit=float(run.cost_limit or 0), estimated_next_cost=None):
                    await uow.workflows.transition(run, "BUDGET_BLOCKED")
                    await uow.commit()
                    return "budget-blocked"
                await uow.workflows.heartbeat(run, lease_owner)
                await uow.workflows.checkpoint(run, lease_owner, f"CHAPTER_{chapter.chapter_no}_DRAFTING")
                await uow.commit()
            async def renew_lease() -> None:
                while True:
                    await asyncio.sleep(20)
                    async with SqlAlchemyUnitOfWork(session_factory) as lease_uow:
                        live = await lease_uow.workflows.get_owned(workflow_id, owner_id, lock=True)
                        if live is None or live.status != "RUNNING" or live.lease_owner != lease_owner:
                            return
                        await lease_uow.workflows.heartbeat(live, lease_owner)
                        await lease_uow.commit()

            chapter_context = context_text
            if rag_enabled:
                try:
                    pack = await NarrativeRetriever(session_factory, master_key=settings.master_key.get_secret_value()).build(
                        project_id=run.project_id, user_id=owner_id, query=chapter.title, chapter_no=chapter.chapter_no,
                    )
                    if pack.text:
                        # Strict budget: the pack may only spend what the
                        # compiled context left in the input budget;
                        # trim evidence first, then structured sections.
                        remaining = max(0, int(budget.input_tokens) - (len(context_text) // 2))
                        pack_text = trim_scene_pack(pack.sections, remaining)
                        if pack_text:
                            chapter_context = f"{pack_text}\n\n{context_text}"
                except Exception:
                    import logging

                    logging.getLogger(__name__).exception("narrative rag scene pack failed; using legacy context")
            renewer = asyncio.create_task(renew_lease())
            try:
                content, rewrite_rounds, _review = await run_writer_editor_loop(
                    provider,
                    writer_model=model_id,
                    editor_model=roles.review[1],
                    editor_provider=role_providers.get(roles.review[0]) if roles.review[0] != roles.write[0] else None,
                    reviser_model=roles.revise[1],
                    reviser_provider=role_providers.get(roles.revise[0]) if roles.revise[0] not in {roles.write[0], roles.review[0]} else None,
                    project_title=project.title,
                    chapter_title=chapter.title,
                    context_text=chapter_context,
                    usage_call_id_factory=usage_call_id,
                    on_usage=record_usage,
                )
            finally:
                renewer.cancel()
                await asyncio.gather(renewer, return_exceptions=True)
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                run = await uow.workflows.get_owned(workflow_id, owner_id)
                if run is None:
                    return "workflow-not-found"
                if should_abort_workflow(run.status):
                    return "cancelled"
                if run.status == "PAUSED":
                    return "paused"
                if superseded(run.status, run.checkpoint):
                    return "superseded"
                if run.lease_owner != lease_owner:
                    return "superseded"
                version = await uow.chapters.append_version(chapter_id=chapter.id, content=content)
                await uow.chapters.set_active_version(chapter.id, version.id)
                # First generated chapter version locks the writing model
                # (no-op once locked; outline import may have won earlier).
                await uow.projects.lock_writing_model(run.project_id, provider=provider_id, model_id=model_id, source="first_chapter")
                await uow.workflows.checkpoint(run, lease_owner, f"CHAPTER_{chapter.chapter_no}_COMMITTED_REWRITES_{rewrite_rounds}")
                await uow.commit()
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            run = await uow.workflows.get_owned(workflow_id, owner_id)
            if run is not None and should_abort_workflow(run.status):
                return "cancelled"
            if run is not None and run.status == "RUNNING":
                await uow.workflows.transition(run, "COMPLETED")
                await uow.commit()
        return "completed"
    except PermissionError:
        # A resume/retry can revoke this delivery's lease while the provider
        # call is in flight. The successor owns recovery; never fail its run.
        return "superseded"
    except Exception as error:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            run = await uow.workflows.get_owned(workflow_id, owner_id)
            if run is not None and run.status in {"RUNNING", "RETRYING", "RECOVERING"}:
                run.last_error = type(error).__name__
                await uow.workflows.transition(run, "FAILED")
                await uow.commit()
        raise
    finally:
        await engine.dispose()


async def healthcheck(payload: dict[str, object]) -> str:
    del payload
    return "ok"


async def sync_all_models(payload: dict[str, object]) -> dict[str, int]:
    import base64
    import json

    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.infrastructure.security.credential_cipher import (
        CredentialCipher,
        derive_key,
    )
    from proseforge.providers.factory import build_provider
    from proseforge.settings import get_settings

    del payload
    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    synced = 0
    failed = 0
    try:
        raw_key = settings.master_key.get_secret_value()
        key = derive_key(raw_key)
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            for credential in await uow.credentials.list_all():
                try:
                    associated = f"{credential.user_id}:{credential.provider}:{credential.id}".encode()
                    secret = json.loads(CredentialCipher(key).decrypt(base64.b64decode(credential.encrypted_payload), associated_data=associated))
                    provider = build_provider(credential.provider, secret["api_key"], secret.get("base_url"))
                    models = await provider.list_models()
                    await uow.model_catalog.upsert(models)
                    await uow.model_catalog.mark_unavailable(credential.provider, {model.model_id for model in models})
                    synced += 1
                except Exception:
                    failed += 1
            await uow.commit()
    finally:
        await engine.dispose()
    return {"synced": synced, "failed": failed}


async def recover_expired(payload: dict[str, object]) -> int:
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.settings import get_settings

    del payload
    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            recovered = await uow.workflows.recover_expired()
            await uow.commit()
            return recovered
    finally:
        await engine.dispose()


async def execute_agent_run(payload: dict[str, object]) -> str:
    """Execute a persisted V3 graph one checkpoint at a time.

    The worker owns task transitions and artifact writes; it never writes a
    ChapterVersion.  Chief Editor output is routed through V2 proposals.
    持久循环、有界并行与真实模型调用在 agent_executor.execute_run（V3-004/005），
    此处仅保留队列入口的薄委托。
    """
    from proseforge.workflows.agent_executor import execute_run

    return await execute_run(payload)


async def generate_chat(payload: dict[str, object]) -> str:
    """Run one durable chat generation task in the worker process."""
    import base64
    import json
    import logging
    import time

    from proseforge.application.conversations.compile_chat_context import (
        CompileChatContext,
    )
    from proseforge.application.conversations.generate_reply import GenerateReply
    from proseforge.application.conversations.search_rounds import (
        run_auto_search,
        web_search_switch_enabled,
    )
    from proseforge.application.conversations.terminal_state import (
        terminal_message_status,
    )
    from proseforge.application.models.cluster_config import resolve_role_models
    from proseforge.application.models.reasoning_policy import resolve_reasoning
    from proseforge.application.models.resolve_model import resolve_capabilities
    from proseforge.application.tools.orchestrator import run_tool_rounds
    from proseforge.application.work.retriever import (
        QUERY_MAX_CHARS,
        NarrativeRetriever,
        narrative_rag_switch_enabled,
        trim_scene_pack,
    )
    from proseforge.context_engine.budgeting import calculate_budget
    from proseforge.domain.conversation.candidates import latest_attempt_per_group
    from proseforge.domain.ports.model_provider import GenerationRequest
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.infrastructure.events.hybrid import HybridEventStream
    from proseforge.infrastructure.security.credential_cipher import (
        CredentialCipher,
        derive_key,
    )
    from proseforge.providers.factory import build_provider
    from proseforge.settings import get_settings

    settings = get_settings()
    logger = logging.getLogger(__name__)
    task_started_at = time.monotonic()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    event_stream = None
    try:
        message_id = str(payload["message_id"])
        user_id = str(payload.get("user_id", ""))
        provider_id = str(payload.get("provider", "openai"))
        model = str(payload.get("model", "gpt-4.1-mini"))
        reasoning_level = str(payload.get("reasoning_level", "auto"))
        event_stream = HybridEventStream(session_factory, settings.redis_url)

        async def fail_message(uow, status: str, reason: str) -> None:
            # 先落库终态再广播 message.failed——SSE live tail 靠终态事件收尾，
            # 缺了它订阅方会永远挂起（d72fb93 引入的早退路径原本不发布）。
            await uow.conversations.set_message_status(message_id, status)
            conversation_id = await uow.conversations.conversation_id_for_message(message_id)
            await uow.commit()
            failed_payload = {"event": "message.failed", "message_id": message_id, "status": status, "reason": reason}
            await event_stream.publish(f"message:{message_id}", failed_payload)
            if conversation_id:
                await event_stream.publish(f"conversation:{conversation_id}", failed_payload)

        # Read-only pre-pass: resolve mode/effective model and gate on
        # credential availability BEFORE the scene pack build -- a missing
        # credential must not pay for an embedding call or leave an orphan
        # retrieval_runs row. The pack build still runs before the claim
        # transaction opens: the retriever commits its own retrieval_runs
        # row, which would deadlock against an open write transaction on
        # sqlite.
        pack_sections: dict[str, str] | None = None
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            pre_message = await uow.conversations.get_message(message_id)
            if pre_message is None:
                return "provider-not-configured"
            # The branch always contains the target message itself, so the
            # visible list is non-empty here -- no separate emptiness check.
            pre_visible = await uow.conversations.list_visible_messages(pre_message.branch_id)
            rag_project_id = await uow.conversations.project_id_for_message(message_id)
            rag_conversation_id = await uow.conversations.conversation_id_for_message(message_id)
            from sqlalchemy import func as _func
            from sqlalchemy import select as _pre_select

            from proseforge.infrastructure.database.models.chapter import (
                ChapterModel as _PreChapterModel,
            )
            from proseforge.infrastructure.database.models.project import (
                ProjectModel as _PreProjectModel,
            )
            project_row = await uow.session.scalar(_pre_select(_PreProjectModel).where(_PreProjectModel.id == rag_project_id))
            project_mode = str(project_row.mode) if project_row is not None else "work"
            # Chat-mode projects are untouched. Work mode resolves the
            # writing-model lock + cluster write role BEFORE the credential
            # gate; reasoning is resolved afterwards off the final model and
            # stays user-adjustable.
            if project_mode == "work":
                locked_ref = (project_row.writing_model_provider, project_row.writing_model_id) if project_row is not None and project_row.model_locked_at else None
                # Cluster config honors the project override (project > global).
                roles = await resolve_role_models(uow, user_id, locked=locked_ref, requested=(provider_id, model), project_id=rag_project_id)
                provider_id, model = roles.write
            if await uow.credentials.get_for_user(user_id, provider_id) is None:
                await fail_message(uow, terminal_message_status(len(pre_message.content)), "provider-not-configured")
                return "provider-not-configured"
            rag_enabled = await narrative_rag_switch_enabled(uow, user_id)
            # Light pre-claim status check: a cancelled or duplicate
            # delivery (status already past PENDING/PARTIAL) never pays
            # the retrieval build; the claim transaction below still
            # arbitrates the real race.
            if pre_message.status not in {"PENDING", "PARTIAL"}:
                rag_enabled = False
            if rag_enabled:
                # Empty-index short-circuit (same guard as the agent
                # executor): chat projects usually have no indexable
                # chapters, so retrieval provably returns nothing — skip
                # the embed spend and the 0-hit retrieval_runs row.
                indexable = int(await uow.session.scalar(
                    _pre_select(_func.count(_PreChapterModel.id)).where(
                        _PreChapterModel.project_id == rag_project_id,
                        _PreChapterModel.active_version_id.isnot(None),
                    )
                ) or 0)
                if not indexable:
                    rag_enabled = False
            latest_user = next((item for item in reversed(pre_visible) if item.id != message_id and item.role == "user"), None)
            # The raw user message is unbounded; a retrieval query is a
            # search key, not a document — cap it before it hits the
            # embedder (API embedders bill/reject on the full text).
            rag_query = str(latest_user.content or "")[:QUERY_MAX_CHARS] if latest_user else ""
        if rag_enabled:
            try:
                pack = await NarrativeRetriever(session_factory, master_key=settings.master_key.get_secret_value()).build(
                    project_id=rag_project_id, user_id=user_id, query=rag_query,
                    conversation_id=rag_conversation_id, message_id=message_id,
                )
                if pack.text:
                    pack_sections = dict(pack.sections)
            except Exception:
                logger.exception("narrative rag scene pack failed message_id=%s; using legacy context", message_id)

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            message = await uow.conversations.get_message(message_id)
            visible = await uow.conversations.list_visible_messages(message.branch_id) if message else []
            # Regenerate keeps rejected candidates on the same branch; only the
            # latest attempt per group may enter the model context, otherwise
            # stale replies pollute the next generation.
            visible = latest_attempt_per_group(visible)
            if message is None or not visible:
                if message:
                    await fail_message(uow, terminal_message_status(len(message.content)), "provider-not-configured")
                return "provider-not-configured"
            project_id = await uow.conversations.project_id_for_message(message_id)
            # Mode/lock/cluster role were already resolved in the pre-pass;
            # the credential is re-fetched here because its payload is
            # decrypted below and the pre-pass gate only proved it existed.
            credential = await uow.credentials.get_for_user(user_id, provider_id)
            if credential is None:
                await fail_message(uow, terminal_message_status(len(message.content)), "provider-not-configured")
                return "provider-not-configured"
            original_status = message.status
            if not await uow.conversations.claim_generation(message_id):
                await uow.commit()
                return "already-running"
            catalog = await uow.model_catalog.get(provider_id, model)  # catalog 为事实
            # 未知模型 → 保守 fallback（source 记录在 message snapshot），绝不让生成崩溃。
            capabilities = resolve_capabilities(catalog)
            try:
                resolution = resolve_reasoning(reasoning_level, capabilities)  # 不支持已在路由层 422
            except ValueError as exc:
                resolution = {"level": reasoning_level, "supported": False, "reason": str(exc), "parameter": None}
            history = visible
            if original_status == "PARTIAL" and message.content:
                # PARTIAL 续写：目标消息内容仅由下方 continue block 追加一次，
                # 从编译历史中剔除，否则 provider 会收到两遍 partial。
                history = [item for item in visible if item.id != message_id]
            # User-uploaded attachments: prefix the parsed file text onto the
            # owning user message's content (token-capped per attachment);
            # messages without attachments pass through untouched.
            from proseforge.application.files.message_attachments import (
                inject_history_attachments,
            )

            history = await inject_history_attachments(uow.session, settings.blob_root, history)
            pack_text: str | None = None
            if pack_sections:
                # Small-window models (8k/16k) hard-fail with a provider
                # 400 when the system blocks alone exceed the context
                # window: trim the pack to what the input budget leaves
                # after the reserve for the compiler's own system blocks.
                # History yields to the pack inside the compiler, so it is
                # not deducted here.
                chat_budget = calculate_budget(capabilities.context_window, capabilities.max_output_tokens)
                pack_text = trim_scene_pack(pack_sections, max(0, int(chat_budget.input_tokens) - CHAT_SCENE_PACK_RESERVE_TOKENS)) or None
            context = await CompileChatContext(uow).execute(
                project_id=project_id, history=history, capabilities=capabilities,
                provider=provider_id, model=model, reasoning=resolution, user_id=user_id,
                mode=project_mode, scene_pack_text=pack_text,
            )
            await uow.conversations.set_message_snapshots(message_id, context.model_snapshot, context.reasoning_snapshot)
            # Server-side intent fallback: one proactive search when the
            # user switch is on and the message smells time-sensitive.
            auto_search_enabled = settings.search_auto_intent_enabled and await web_search_switch_enabled(uow, user_id)
            conversation_id = await uow.conversations.conversation_id_for_message(message_id) if auto_search_enabled else None
            raw = derive_key(settings.master_key.get_secret_value())
            associated = f"{user_id}:{provider_id}:{credential.id}".encode()
            secret = json.loads(CredentialCipher(raw).decrypt(base64.b64decode(credential.encrypted_payload), associated_data=associated))
            await uow.commit()  # ContextSnapshot 与 message snapshot 字段在生成开始前落库（不可变）
        base_url = secret.get("base_url")
        try:
            provider = build_provider(provider_id, secret["api_key"], base_url=base_url)
        except KeyError:
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                message = await uow.conversations.get_message(message_id)
                if message:
                    await fail_message(uow, terminal_message_status(len(message.content)), "provider-not-supported")
            return "provider-not-supported"
        system_blocks: list[dict] = list(context.system_blocks)
        if auto_search_enabled:
            user_message = next((item for item in reversed(visible) if item.id != message_id and item.role == "user"), None)
            user_text = str(user_message.content or "") if user_message else ""
            system_blocks = await run_auto_search(
                system_blocks=system_blocks,
                user_text=user_text,
                settings=settings,
                event_stream=event_stream,
                message_id=message_id,
                conversation_id=conversation_id,
            )
        input_blocks = [dict(block) for block in context.messages]
        if message is not None and original_status == "PARTIAL" and message.content:
            input_blocks.append({"role": "assistant", "text": message.content})
            input_blocks.append({"role": "user", "text": "Continue from the saved partial response without repeating existing text."})
        request = GenerationRequest(
            model=model,
            system_blocks=system_blocks,
            input_blocks=tuple(input_blocks),
            max_output_tokens=capabilities.max_output_tokens,
            reasoning=resolution.get("provider_parameter"),
        )
        logger.info("generate_chat context compiled message_id=%s context_compile_ms=%d", message_id, int((time.monotonic() - task_started_at) * 1000))
        await GenerateReply(lambda: SqlAlchemyUnitOfWork(session_factory), provider, event_stream).execute(message_id=message_id, request=request, user_id=user_id, provider=provider_id, model=model)
        # Post-completion tool rounds (unified ```tool: fence contract).
        # Runs only on a COMPLETED message; tool failures are written into
        # the result block, never raised here.
        await run_tool_rounds(
            session_factory=session_factory,
            event_stream=event_stream,
            provider=provider,
            message_id=message_id,
            user_id=user_id,
            provider_id=provider_id,
            model=model,
            system_blocks=system_blocks,
            base_input_blocks=input_blocks,
            max_output_tokens=capabilities.max_output_tokens,
            reasoning=resolution.get("provider_parameter"),
            settings=settings,
        )
        return "completed"
    except Exception:
        # Fallback terminal state (same rule as generate_novel): any exception
        # after the claim commit but before GenerateReply -- build_provider,
        # run_auto_search, context compilation -- would otherwise strand the
        # message in STREAMING/PENDING, and a celery retry would then exit
        # silently on the already-consumed claim. GenerateReply persists its
        # own terminal status before raising, so the status guard below skips
        # errors that already landed a terminal state. message_id/fail_message
        # are bound whenever event_stream exists (both assigned earlier).
        if event_stream is not None:
            try:
                async with SqlAlchemyUnitOfWork(session_factory) as uow:
                    stuck = await uow.conversations.get_message(message_id)
                    if stuck is not None and stuck.status in {"PENDING", "STREAMING"}:
                        await fail_message(uow, terminal_message_status(len(stuck.content)), "internal-error")
            except Exception:
                logger.exception("generate_chat fallback fail_message failed message_id=%s", message_id)
        raise
    finally:
        if event_stream is not None:
            try:
                await event_stream.aflush()
            except Exception:
                logger.exception("generate_chat final event flush failed message_id=%s", locals().get("message_id"))
        await engine.dispose()


async def index_retrieval_document(payload: dict[str, object]) -> str:
    """Thin queue entry for narrative RAG indexing; logic lives in application/retrieval/indexing.py."""
    from proseforge.application.retrieval.indexing import run_index_job

    return await run_index_job(payload)


async def summarize_chapter_task(payload: dict[str, object]) -> dict[str, object]:
    """Thin queue entry for chapter summary + character extraction."""
    from proseforge.application.work.summarize_chapter import run_summarize_job

    return await run_summarize_job(payload)


async def rollup_recap_task(payload: dict[str, object]) -> dict[str, object]:
    """Thin queue entry for volume/book/era recap rollups (memory pyramid)."""
    from proseforge.application.work.rollup_recap import run_rollup_job

    return await run_rollup_job(payload)


async def sweep_pending_retrieval_jobs(payload: dict[str, object]) -> int:
    """Queue entry for the retrieval pending-job sweeper (celery beat).

    Re-enqueues retrieval_jobs stranded in pending between the business
    commit and the queue dispatch; replay is idempotent in the worker.
    """
    from proseforge.application.retrieval.indexing import sweep_pending_jobs
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.infrastructure.tasks.celery import CeleryTaskQueue
    from proseforge.settings import get_settings

    del payload
    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            return await sweep_pending_jobs(uow, CeleryTaskQueue())
    finally:
        await engine.dispose()


HANDLERS: dict[str, TaskHandler] = {
    "proseforge.workflows.generate_novel": generate_novel,
    "proseforge.chat.generate": generate_chat,
    "proseforge.providers.sync_all_models": sync_all_models,
    "proseforge.workflows.recover_expired": recover_expired,
    "proseforge.healthcheck": healthcheck,
    "proseforge.agents.execute_run": execute_agent_run,
    "proseforge.workflows.execute_v2_run": execute_v2_run,
    "proseforge.retrieval.index_document": index_retrieval_document,
    "proseforge.work.summarize_chapter": summarize_chapter_task,
    "proseforge.work.rollup_recap": rollup_recap_task,
}
