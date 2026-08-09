"""generate_chat 兜底异常处理回归（真 sqlite）。

claim 提交后、GenerateReply 之前的任何异常（build_provider、run_auto_search
等）曾让消息永久卡 STREAMING/PENDING——celery 重试时 claim 已被消费，任务
静默 "already-running" 退出。修复后 generate_chat 主体有兜底 except：消息
仍处非终态时先 fail_message(internal-error) 落终态再抛出。GenerateReply
自己已落终态后抛出的异常（如流中断 → PARTIAL）不得被兜底覆盖。
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest

from proseforge.domain.conversation.entity import Conversation
from proseforge.domain.ports.model_provider import GenerationEvent, ProviderModel
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.conversation import ConversationModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings
from proseforge.workflows.tasks import generate_chat

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def settings_env(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}"
    monkeypatch.setenv("PROSEFORGE_DATABASE_URL", database_url)
    monkeypatch.setenv("PROSEFORGE_RUNTIME_PROFILE", "native")
    monkeypatch.setenv("PROSEFORGE_MASTER_KEY", MASTER_KEY)
    get_settings.cache_clear()
    yield Settings(
        database_url=database_url, runtime_profile="native", master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"), backup_root=str(tmp_path / "backups"),
    )
    get_settings.cache_clear()


async def _seed_chat(settings: Settings) -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"writer-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            project = Project.create(owner_id=user.id, slug=f"proj-{uuid.uuid4().hex[:8]}", title="Novel", mode="chat")
            await uow.projects.add(project)
            credential_id = f"cred-{uuid.uuid4().hex[:8]}"
            associated = f"{user.id}:openai:{credential_id}".encode()
            encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(
                json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated
            )
            await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            await uow.model_catalog.upsert([
                ProviderModel("openai", "gpt-a", "GPT A", {}, context_window=8192, max_output_tokens=1024)
            ])
            conversation = Conversation.create(project.id, "Chat")
            main = await uow.conversations.create(conversation)
            await uow.conversations.append_message(main.id, "user", "继续写烛龙之战")
            target = await uow.conversations.append_message(main.id, "assistant", "", None, "PENDING")
            uow.session.add(ConversationModel(id=target.id, project_id=project.id, title="message-stream"))
            await uow.commit()
            return {"user_id": user.id, "message_id": target.id}
    finally:
        await engine.dispose()


async def _message_status(settings: Settings, message_id: str) -> str | None:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            message = await uow.conversations.get_message(message_id)
            return message.status if message else None
    finally:
        await engine.dispose()


def _payload(seeded: dict[str, str]) -> dict[str, str]:
    return {
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a", "reasoning_level": "auto",
    }


def _patch_retriever_empty(monkeypatch) -> None:
    # RAG 开关默认 ON：不打补丁会去走真实 embedding 调用。返回空 pack 让
    # 本文件的测试聚焦在兜底异常处理上（RAG 接线由 test_rag_wiring 覆盖）。
    from types import SimpleNamespace

    async def fake_build(self, **kwargs):
        return SimpleNamespace(text="", sections={}, evidence=[], run_id="r1", token_cost=1)

    monkeypatch.setattr("proseforge.application.work.retriever.NarrativeRetriever.build", fake_build)


@pytest.mark.asyncio
async def test_pre_generation_error_lands_failed_instead_of_stuck_streaming(settings_env, monkeypatch):
    # build_provider 在 claim 提交之后抛错：兜底 except 必须把 STREAMING 的
    # 消息翻成 FAILED（internal-error）再抛出，不留卡死消息。
    seeded = await _seed_chat(settings_env)
    _patch_retriever_empty(monkeypatch)

    def _exploding_factory(*args, **kwargs):
        raise RuntimeError("provider factory exploded")

    monkeypatch.setattr("proseforge.providers.factory.build_provider", _exploding_factory)

    with pytest.raises(RuntimeError, match="provider factory exploded"):
        await generate_chat(_payload(seeded))

    assert await _message_status(settings_env, seeded["message_id"]) == "FAILED"


@pytest.mark.asyncio
async def test_stream_interruption_keeps_generate_reply_terminal_status(settings_env, monkeypatch):
    # GenerateReply 已自行落 PARTIAL 再抛出：兜底不得覆盖它的终态。
    seeded = await _seed_chat(settings_env)
    _patch_retriever_empty(monkeypatch)

    class InterruptProvider:
        async def stream(self, request):
            yield GenerationEvent("content.delta", "半句")
            raise RuntimeError("network interruption")

    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: InterruptProvider())

    with pytest.raises(Exception, match="network interruption"):
        await generate_chat(_payload(seeded))

    assert await _message_status(settings_env, seeded["message_id"]) == "PARTIAL"


@pytest.mark.asyncio
async def test_unsupported_provider_still_returns_via_existing_path(settings_env, monkeypatch):
    # 既有 KeyError 路径（provider-not-supported）正常 return，不被兜底改变。
    seeded = await _seed_chat(settings_env)
    _patch_retriever_empty(monkeypatch)
    credential_key_error = KeyError("api_key")

    def _key_error_factory(*args, **kwargs):
        raise credential_key_error

    monkeypatch.setattr("proseforge.providers.factory.build_provider", _key_error_factory)

    result = await generate_chat(_payload(seeded))

    assert result == "provider-not-supported"
    assert await _message_status(settings_env, seeded["message_id"]) == "FAILED"
