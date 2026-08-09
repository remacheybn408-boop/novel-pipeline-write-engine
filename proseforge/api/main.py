from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from proseforge.api.errors import domain_error_handler
from proseforge.api.middleware import AgentRateLimitMiddleware, CorrelationIdMiddleware
from proseforge.api.routes.agent_runs import router as agent_runs_router
from proseforge.api.routes.auth import router as auth_router
from proseforge.api.routes.branches import router as branches_router
from proseforge.api.routes.chapters import router as chapters_router
from proseforge.api.routes.chapters import v2_router as chapters_v2_router
from proseforge.api.routes.characters import router as characters_router
from proseforge.api.routes.conflicts import router as conflicts_router
from proseforge.api.routes.context import preview_router as context_preview_router
from proseforge.api.routes.context import router as context_router
from proseforge.api.routes.conversations import router as conversations_router
from proseforge.api.routes.credentials import router as credentials_router
from proseforge.api.routes.embedding_settings import router as embedding_settings_router
from proseforge.api.routes.exports import router as exports_router
from proseforge.api.routes.files import router as files_router
from proseforge.api.routes.health import router as health_router
from proseforge.api.routes.knowledge import router as knowledge_router
from proseforge.api.routes.logs import router as logs_router
from proseforge.api.routes.maintenance import router as maintenance_router
from proseforge.api.routes.model_capabilities import router as model_capabilities_router
from proseforge.api.routes.model_profiles import router as model_profiles_router
from proseforge.api.routes.outlines import router as outlines_router
from proseforge.api.routes.plugins import router as plugins_router
from proseforge.api.routes.projects import router as projects_router
from proseforge.api.routes.providers import router as providers_router
from proseforge.api.routes.retrieval import router as retrieval_router
from proseforge.api.routes.reviews import router as reviews_router
from proseforge.api.routes.revisions import router as revisions_router
from proseforge.api.routes.runtime import router as runtime_router
from proseforge.api.routes.scene_state import router as scene_state_router
from proseforge.api.routes.static_web import router as static_web_router
from proseforge.api.routes.story_bible import router as story_bible_router
from proseforge.api.routes.tools import router as tools_router
from proseforge.api.routes.usage import router as usage_router
from proseforge.api.routes.workflow_definitions import (
    router as workflow_definitions_router,
)
from proseforge.api.routes.workflow_runs import router as workflow_runs_router
from proseforge.api.routes.workflows import router as workflows_router
from proseforge.api.routes.writing_status import router as writing_status_router
from proseforge.application.auth.service import AuthService
from proseforge.domain.common.errors import DomainError
from proseforge.infrastructure.database.bootstrap import ensure_schema
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.events.hybrid import HybridEventStream
from proseforge.infrastructure.scheduler.local import LocalScheduler
from proseforge.infrastructure.tasks.factory import create_task_queue
from proseforge.providers.agnes import AgnesProvider
from proseforge.providers.anthropic import AnthropicProvider
from proseforge.providers.baichuan import BaichuanProvider
from proseforge.providers.baidu import BaiduProvider
from proseforge.providers.cerebras import CerebrasProvider
from proseforge.providers.cohere import CohereProvider
from proseforge.providers.dashscope import DashScopeProvider
from proseforge.providers.deepinfra import DeepInfraProvider
from proseforge.providers.deepseek import DeepSeekProvider
from proseforge.providers.fireworks import FireworksProvider
from proseforge.providers.google import GoogleProvider
from proseforge.providers.groq import GroqProvider
from proseforge.providers.iflytek import IFlytekProvider
from proseforge.providers.kimi import KimiProvider
from proseforge.providers.minimax import MiniMaxProvider
from proseforge.providers.mistral import MistralProvider
from proseforge.providers.novita import NovitaProvider
from proseforge.providers.ollama import OllamaProvider
from proseforge.providers.openai import OpenAIProvider
from proseforge.providers.openrouter import OpenRouterProvider
from proseforge.providers.perplexity import PerplexityProvider
from proseforge.providers.registry import ProviderRegistry
from proseforge.providers.sambanova import SambaNovaProvider
from proseforge.providers.sensenova import SenseNovaProvider
from proseforge.providers.siliconflow import SiliconFlowProvider
from proseforge.providers.stepfun import StepFunProvider
from proseforge.providers.tencent import TencentProvider
from proseforge.providers.together import TogetherProvider
from proseforge.providers.vllm import VLLMProvider
from proseforge.providers.volcengine import VolcEngineProvider
from proseforge.providers.xai import XAIProvider
from proseforge.providers.yi import YiProvider
from proseforge.providers.zhipu import ZhipuProvider
from proseforge.runtime.bootstrap import bootstrap_runtime
from proseforge.runtime.factory import create_runtime
from proseforge.runtime.lifecycle import RuntimeLifecycle
from proseforge.runtime.logging import setup_logging
from proseforge.runtime.paths import resolve_paths
from proseforge.runtime.profile import RuntimeProfile, capabilities_for
from proseforge.settings import Settings, get_settings


class _NoopScheduler:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _bootstrap_runtime(settings: Settings, application: FastAPI) -> None:
    env = dict(os.environ)
    if settings.data_dir:
        env["PROSEFORGE_DATA_DIR"] = settings.data_dir
    env["PROSEFORGE_DATABASE_URL"] = settings.database_url
    env["PROSEFORGE_BLOB_ROOT"] = settings.blob_root
    env["PROSEFORGE_BACKUP_ROOT"] = settings.backup_root
    profile = RuntimeProfile(settings.runtime_profile)
    paths = resolve_paths(profile, env)
    bootstrap_runtime(paths, profile)
    # File logging covers every profile (server included) and is idempotent.
    setup_logging(paths.log_dir)
    application.state.runtime_paths = paths
    ensure_schema(settings)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        await application.state.lifecycle.start()
        try:
            yield
        finally:
            await application.state.lifecycle.stop()

    application = FastAPI(title="ProseForge API", version="1.5.0", lifespan=lifespan)
    application.add_exception_handler(DomainError, domain_error_handler)
    application.add_middleware(CorrelationIdMiddleware)
    application.add_middleware(
        AgentRateLimitMiddleware,
        read_per_minute=resolved.agent_rate_limit_read_per_minute,
        write_per_minute=resolved.agent_rate_limit_write_per_minute,
        auth_per_minute=resolved.auth_rate_limit_per_minute,
    )
    application.state.settings = resolved
    application.state.runtime = create_runtime(resolved)
    application.state.auth = AuthService(
        resolved.jwt_secret.get_secret_value(), token_minutes=resolved.session_token_minutes
    )
    application.state.engine, application.state.session_factory = create_engine_and_sessionmaker(resolved)
    application.state.event_stream = HybridEventStream(application.state.session_factory, resolved.redis_url)
    application.state.queue = create_task_queue(resolved, application.state.session_factory)

    last_index_sweep = 0.0  # monotonic gate: the retrieval sweeper runs every 5 minutes, not every tick
    last_message_sweep = 0.0  # monotonic gate: the swarm-message sweeper runs every 5 minutes, not every tick

    async def maintenance_tick() -> None:
        nonlocal last_index_sweep, last_message_sweep
        recover_expired = getattr(application.state.queue, "recover_expired", None)
        if recover_expired is not None:
            await recover_expired()
        # v2：租约过期节点重排（RECOVERING → QUEUED），并为 QUEUED 的
        # definition run 补入队执行器；入队失败留待下一 tick。
        from proseforge.application.workflows.recover_run import (
            queued_definition_run_ids,
            recover_expired_workflow_nodes,
        )
        from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
        from proseforge.workflows.v2_tasks import EXECUTE_V2_RUN_TASK

        async with SqlAlchemyUnitOfWork(application.state.session_factory) as uow:
            await recover_expired_workflow_nodes(uow)
            queued = await queued_definition_run_ids(uow)
            await uow.commit()
        for run_id in queued:
            try:
                await application.state.queue.enqueue(EXECUTE_V2_RUN_TASK, {"run_id": run_id})
            except Exception:
                break

        # Narrative RAG sweeper: re-dispatch retrieval_jobs stranded in
        # pending between the business commit and the queue enqueue.
        import time

        now = time.monotonic()
        if now - last_index_sweep >= 300:
            last_index_sweep = now
            from proseforge.application.retrieval.indexing import sweep_pending_jobs

            async with SqlAlchemyUnitOfWork(application.state.session_factory) as sweep_uow:
                await sweep_pending_jobs(sweep_uow, application.state.queue)

        # Swarm-message sweeper: replay the writeback for assistant
        # messages stranded non-terminal (terminal run, failed writeback
        # commit) and fail RUNNING runs whose executor died mid-flight.
        if now - last_message_sweep >= 300:
            last_message_sweep = now
            from proseforge.application.messages.sweeper import sweep_stale_run_messages

            await sweep_stale_run_messages(application.state.session_factory, resolved)


    capabilities = capabilities_for(RuntimeProfile(resolved.runtime_profile))
    scheduler = (
        LocalScheduler(
            maintenance_tick,
            interval_seconds=max(1.0, resolved.native_queue_poll_seconds),
        )
        if capabilities.queue == "local"
        else _NoopScheduler()
    )
    application.state.lifecycle = RuntimeLifecycle(
        bootstrap=lambda: _bootstrap_runtime(resolved, application),
        queue=application.state.queue,
        scheduler=scheduler,
        engine=application.state.engine,
    )

    registry = ProviderRegistry()
    for provider in (
        OpenAIProvider(""), AnthropicProvider(""), GoogleProvider(""), DeepSeekProvider(), KimiProvider(),
        DashScopeProvider(), ZhipuProvider(), VolcEngineProvider(), BaiduProvider(), TencentProvider(),
        MiniMaxProvider(), XAIProvider(), MistralProvider(), CohereProvider(), OllamaProvider(), VLLMProvider(),
        StepFunProvider(), YiProvider(), BaichuanProvider(), IFlytekProvider(), SenseNovaProvider(),
        SiliconFlowProvider(), OpenRouterProvider(), GroqProvider(), TogetherProvider(), FireworksProvider(),
        PerplexityProvider(), CerebrasProvider(), SambaNovaProvider(), DeepInfraProvider(), NovitaProvider(),
        AgnesProvider(),
    ):
        registry.register(provider)
    application.state.provider_registry = registry
    application.state.model_catalog = {}
    application.include_router(health_router)
    application.include_router(model_capabilities_router)
    application.include_router(auth_router)
    application.include_router(branches_router)
    application.include_router(projects_router)
    application.include_router(conversations_router)
    application.include_router(providers_router)
    application.include_router(workflows_router)
    application.include_router(workflow_definitions_router)
    application.include_router(workflow_runs_router)
    application.include_router(files_router)
    application.include_router(chapters_router)
    application.include_router(chapters_v2_router)
    application.include_router(characters_router)
    application.include_router(knowledge_router)
    application.include_router(exports_router)
    application.include_router(credentials_router)
    application.include_router(embedding_settings_router)
    application.include_router(outlines_router)
    application.include_router(plugins_router)
    application.include_router(context_router)
    application.include_router(conflicts_router)
    application.include_router(context_preview_router)
    application.include_router(model_profiles_router)
    application.include_router(maintenance_router)
    application.include_router(usage_router)
    application.include_router(tools_router)
    application.include_router(runtime_router)
    application.include_router(retrieval_router)
    application.include_router(scene_state_router)
    application.include_router(story_bible_router)
    application.include_router(revisions_router)
    application.include_router(reviews_router)
    application.include_router(agent_runs_router)
    application.include_router(writing_status_router)
    application.include_router(logs_router)
    application.include_router(static_web_router)
    return application


app = create_app()


def get_auth_service() -> AuthService:
    return app.state.auth
