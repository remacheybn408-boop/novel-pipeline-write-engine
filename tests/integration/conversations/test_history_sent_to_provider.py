from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from proseforge.domain.common.ids import new_id
from proseforge.domain.conversation.entity import Conversation
from proseforge.domain.ports.model_provider import GenerationEvent, ProviderModel
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.conversation import (
    ConversationEventModel,
    ConversationModel,
    MessageModel,
)
from proseforge.infrastructure.database.models.remaining import ContextSnapshotModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings
from proseforge.workflows.tasks import generate_chat

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


class SpyProvider:
    provider_id = "openai"

    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield GenerationEvent("content.delta", "暗")
        yield GenerationEvent("content.delta", "涌")


@pytest.fixture()
def chat_settings(tmp_path, monkeypatch):
    database_url = os.environ.get("PROSEFORGE_TEST_DATABASE_URL")
    profile = "test" if database_url else "native"
    if not database_url:
        database_url = f"sqlite+aiosqlite:///{(tmp_path / 'chat.db').as_posix()}"
    monkeypatch.setenv("PROSEFORGE_DATABASE_URL", database_url)
    monkeypatch.setenv("PROSEFORGE_RUNTIME_PROFILE", profile)
    monkeypatch.setenv("PROSEFORGE_MASTER_KEY", MASTER_KEY)
    get_settings.cache_clear()
    yield Settings(
        database_url=database_url,
        runtime_profile=profile,
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    get_settings.cache_clear()


async def _seed(settings: Settings, *, with_catalog: bool = True, with_credential: bool = True, partial_content: str = "") -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"writer-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            project = Project.create(owner_id=user.id, slug=f"novel-{uuid.uuid4().hex[:8]}", title="Novel")
            await uow.projects.add(project)
            if with_credential:
                credential_id = f"cred-{uuid.uuid4().hex[:8]}"
                associated = f"{user.id}:openai:{credential_id}".encode()
                encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(
                    json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated
                )
                await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            if with_catalog:
                await uow.model_catalog.upsert([
                    ProviderModel("openai", "gpt-test", "GPT Test", {"reasoning": True, "reasoning_parameter": "effort"}, context_window=2048, max_output_tokens=333)
                ])
            conversation = Conversation.create(project.id, "Chat")
            main = await uow.conversations.create(conversation)
            await uow.conversations.append_message(main.id, "user", "第一章写得再暗一点")
            second = await uow.conversations.append_message(main.id, "assistant", "好的，基调压暗")
            await uow.conversations.append_message(main.id, "user", "主线分支的第三条")
            fork = await uow.conversations.fork(conversation.id, second.id, "alternative")
            await uow.conversations.append_message(fork.id, "user", "分叉后的新问题")
            target_status = "PARTIAL" if partial_content else "PENDING"
            target = await uow.conversations.append_message(fork.id, "assistant", partial_content, None, target_status)
            # conversation_events.conversation_id doubles as the durable stream key and
            # also carries message ids (message:{id} streams); the FK to conversations.id
            # requires a parent row for every stream key written in this test.
            uow.session.add(ConversationModel(id=target.id, project_id=project.id, title="message-stream"))
            fact_id = new_id()
            now = datetime.now(UTC)
            uow.session.add(StoryBibleEntryModel(
                id=fact_id, project_id=project.id, kind="character", key="烛龙",
                value_json=json.dumps({"note": "睁眼为昼"}, ensure_ascii=False),
                status="active", confidence=1.0, source="user", pinned=True, version=1, created_at=now, updated_at=now,
            ))
            await uow.commit()
            return {"user_id": user.id, "project_id": project.id, "conversation_id": conversation.id, "message_id": target.id, "fact_id": fact_id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_full_branch_history_is_sent_to_provider(chat_settings, monkeypatch):
    seeded = await _seed(chat_settings)
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"],
        "user_id": seeded["user_id"],
        "provider": "openai",
        "model": "gpt-test",
        "reasoning_level": "high",
    })

    assert result == "completed"
    assert len(spy.requests) == 1
    request = spy.requests[0]
    texts = [block["text"] for block in request.input_blocks]
    assert texts == ["第一章写得再暗一点", "好的，基调压暗", "分叉后的新问题"]  # 全分支历史，含 fork 前祖先
    assert "主线分支的第三条" not in texts  # fork 点之后的主分支消息不可见
    assert request.system_blocks, "system blocks must not be empty"
    assert any("烛龙" in str(block.get("text", "")) for block in request.system_blocks)  # pinned story fact 注入
    assert request.max_output_tokens == 333  # catalog 值
    assert request.reasoning is not None and "effort" in str(request.reasoning)

    engine, factory = create_engine_and_sessionmaker(chat_settings)
    try:
        async with factory() as session:
            # 共享 PG 上数据跨用例共存，快照与事件都必须按本用例范围过滤
            snapshots = list((await session.scalars(
                select(ContextSnapshotModel).where(ContextSnapshotModel.project_id == seeded["project_id"])
            )).all())
            assert len(snapshots) == 1
            snapshot_payload = json.loads(snapshots[0].payload)
            assert seeded["fact_id"] in snapshot_payload["injected_fact_ids"]
            assert "omitted" in snapshot_payload
            assert snapshot_payload["message_ids"], "snapshot must record the sent history"

            row = await session.get(MessageModel, seeded["message_id"])
            model_snapshot = json.loads(row.model_snapshot_json)
            assert model_snapshot["model"] == "gpt-test"
            assert model_snapshot["source"] == "catalog"
            assert model_snapshot["context_snapshot_id"] == snapshots[0].id
            reasoning_snapshot = json.loads(row.reasoning_snapshot_json)
            assert reasoning_snapshot["level"] == "high"
            assert row.content_hash == hashlib.sha256("暗涌".encode()).hexdigest()

            events = list((await session.scalars(
                select(ConversationEventModel)
                .where(ConversationEventModel.conversation_id == seeded["conversation_id"])
                .order_by(ConversationEventModel.event_sequence)
            )).all())
            event_types = [event.event_type for event in events]
            assert "message.started" in event_types
            assert "message.completed" in event_types
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_model_uses_conservative_fallback(chat_settings, monkeypatch):
    seeded = await _seed(chat_settings, with_catalog=False)
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"],
        "user_id": seeded["user_id"],
        "provider": "openai",
        "model": "gpt-unknown",
        "reasoning_level": "high",
    })

    assert result == "completed"  # 未知模型不得让生成崩溃
    request = spy.requests[0]
    assert request.max_output_tokens == 8192  # fallback cap (raised from 1024: reasoning burns small budgets)

    engine, factory = create_engine_and_sessionmaker(chat_settings)
    try:
        async with factory() as session:
            row = await session.get(MessageModel, seeded["message_id"])
            model_snapshot = json.loads(row.model_snapshot_json)
            assert model_snapshot["source"] == "fallback"
            assert model_snapshot["context_window"] == 8192
            reasoning_snapshot = json.loads(row.reasoning_snapshot_json)
            assert reasoning_snapshot["level"] == "high"
            assert reasoning_snapshot["supported"] is False
    finally:
        await engine.dispose()


async def _stream_events(settings: Settings, stream_key: str) -> list[tuple[str, dict]]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with factory() as session:
            rows = list((await session.scalars(
                select(ConversationEventModel)
                .where(ConversationEventModel.conversation_id == stream_key)
                .order_by(ConversationEventModel.event_sequence)
            )).all())
            return [(row.event_type, json.loads(row.payload)) for row in rows]
    finally:
        await engine.dispose()


async def _persisted_status(settings: Settings, message_id: str) -> str:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with factory() as session:
            row = await session.get(MessageModel, message_id)
            return row.status
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_resume_sends_partial_content_exactly_once(chat_settings, monkeypatch):
    seeded = await _seed(chat_settings, partial_content="夜色压下来，")
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"],
        "user_id": seeded["user_id"],
        "provider": "openai",
        "model": "gpt-test",
        "reasoning_level": "high",
    })

    assert result == "completed"
    assert len(spy.requests) == 1
    blocks = list(spy.requests[0].input_blocks)
    texts = [block["text"] for block in blocks]
    # 历史 + 恰好一个 partial assistant block + continue 指令，partial 不得出现两遍
    assert texts[:-2] == ["第一章写得再暗一点", "好的，基调压暗", "分叉后的新问题"]
    assert texts.count("夜色压下来，") == 1
    assert blocks[-2] == {"role": "assistant", "text": "夜色压下来，"}
    assert blocks[-1]["role"] == "user"
    assert "Continue from the saved partial response" in blocks[-1]["text"]

    engine, factory = create_engine_and_sessionmaker(chat_settings)
    try:
        async with factory() as session:
            # 共享 PG 上快照跨用例共存，必须按本用例项目过滤（sqlite 单文件天然隔离）
            snapshots = list((await session.scalars(
                select(ContextSnapshotModel).where(ContextSnapshotModel.project_id == seeded["project_id"])
            )).all())
            assert len(snapshots) == 1
            snapshot_payload = json.loads(snapshots[0].payload)
            assert seeded["message_id"] not in snapshot_payload["message_ids"]  # 目标 partial 不进编译历史
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provider_not_configured_publishes_terminal_failed_event(chat_settings):
    seeded = await _seed(chat_settings, with_credential=False)

    result = await generate_chat({
        "message_id": seeded["message_id"],
        "user_id": seeded["user_id"],
        "provider": "openai",
        "model": "gpt-test",
        "reasoning_level": "high",
    })

    assert result == "provider-not-configured"
    # 先落库终态，再广播：广播 payload 的 status 与已提交终态一致
    assert await _persisted_status(chat_settings, seeded["message_id"]) == "FAILED"
    for stream_key in (seeded["message_id"], seeded["conversation_id"]):
        events = await _stream_events(chat_settings, stream_key)
        failed = [payload for event_type, payload in events if event_type == "message.failed"]
        assert len(failed) == 1, f"stream {stream_key} must end with exactly one message.failed"
        assert failed[0]["message_id"] == seeded["message_id"]
        assert failed[0]["status"] == "FAILED"
        assert failed[0]["reason"] == "provider-not-configured"


@pytest.mark.asyncio
async def test_provider_not_supported_publishes_terminal_failed_event(chat_settings, monkeypatch):
    seeded = await _seed(chat_settings)

    def _unknown_provider(*args, **kwargs):
        raise KeyError("openai")

    monkeypatch.setattr("proseforge.providers.factory.build_provider", _unknown_provider)

    result = await generate_chat({
        "message_id": seeded["message_id"],
        "user_id": seeded["user_id"],
        "provider": "openai",
        "model": "gpt-test",
        "reasoning_level": "high",
    })

    assert result == "provider-not-supported"
    assert await _persisted_status(chat_settings, seeded["message_id"]) == "FAILED"
    for stream_key in (seeded["message_id"], seeded["conversation_id"]):
        events = await _stream_events(chat_settings, stream_key)
        failed = [payload for event_type, payload in events if event_type == "message.failed"]
        assert len(failed) == 1, f"stream {stream_key} must end with exactly one message.failed"
        assert failed[0]["message_id"] == seeded["message_id"]
        assert failed[0]["status"] == "FAILED"
        assert failed[0]["reason"] == "provider-not-supported"


@pytest.mark.asyncio
async def test_per_message_model_switch_records_distinct_snapshots(chat_settings, monkeypatch):
    seeded = await _seed(chat_settings)
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    # 同一分支追加第二个 catalog 模型与第二条 assistant 目标消息
    engine, factory = create_engine_and_sessionmaker(chat_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            await uow.model_catalog.upsert([
                ProviderModel("openai", "gpt-alt", "GPT Alt", {"reasoning": False}, context_window=4096, max_output_tokens=777)
            ])
            first = await uow.conversations.get_message(seeded["message_id"])
            second = await uow.conversations.append_message(first.branch_id, "assistant", "", None, "PENDING")
            # Same stream-key parent row as in _seed: the second message gets
            # its own durable message:{id} event stream.
            uow.session.add(ConversationModel(id=second.id, project_id=seeded["project_id"], title="message-stream"))
            await uow.commit()
            second_id = second.id
    finally:
        await engine.dispose()

    first_result = await generate_chat({
        "message_id": seeded["message_id"],
        "user_id": seeded["user_id"],
        "provider": "openai",
        "model": "gpt-test",
        "reasoning_level": "auto",
    })
    second_result = await generate_chat({
        "message_id": second_id,
        "user_id": seeded["user_id"],
        "provider": "openai",
        "model": "gpt-alt",
        "reasoning_level": "auto",
    })

    assert first_result == "completed" and second_result == "completed"
    assert spy.requests[-1].max_output_tokens == 777  # 第二次生成走 gpt-alt 的 catalog 上限

    engine, factory = create_engine_and_sessionmaker(chat_settings)
    try:
        async with factory() as session:
            first_row = await session.get(MessageModel, seeded["message_id"])
            second_row = await session.get(MessageModel, second_id)
            first_snapshot = json.loads(first_row.model_snapshot_json)
            second_snapshot = json.loads(second_row.model_snapshot_json)
            # 逐消息切模型：同分支两条消息各自记录自己的 model_snapshot
            assert first_snapshot["model"] == "gpt-test"
            assert first_snapshot["context_window"] == 2048
            assert first_snapshot["max_output_tokens"] == 333
            assert first_snapshot["source"] == "catalog"
            assert second_snapshot["model"] == "gpt-alt"
            assert second_snapshot["context_window"] == 4096
            assert second_snapshot["max_output_tokens"] == 777
            assert second_snapshot["source"] == "catalog"
    finally:
        await engine.dispose()
