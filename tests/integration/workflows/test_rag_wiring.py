"""Scene-pack wiring in generate_novel / generate_chat: switch off keeps
the legacy path, retriever failures degrade, switch on injects the pack.
NarrativeRetriever.build is stubbed; providers stubbed (no network)."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from proseforge.application.work.retriever import (
    NARRATIVE_RAG_SKILL_KEY,
    QUERY_MAX_CHARS,
)
from proseforge.domain.chapter.entity import Chapter
from proseforge.domain.common.ids import new_id
from proseforge.domain.conversation.entity import Conversation
from proseforge.domain.ports.model_provider import GenerationEvent, ProviderModel
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.conversation import ConversationModel
from proseforge.infrastructure.database.models.plugin import UserBuiltinSkillStateModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings
from proseforge.workflows.tasks import generate_chat, generate_novel

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


def _fake_pack(text: str):
    sections = {"worldview": text, "current_state": "", "constraints": "", "evidence": ""}
    return SimpleNamespace(text=f"[世界观与设定]\n{text}", sections=sections, evidence=[], run_id="r1", token_cost=1)


def _patch_retriever(monkeypatch, *, text: str = "[场景包] 测试场景", calls: list | None = None, error: Exception | None = None):
    async def fake_build(self, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        if error is not None:
            raise error
        return _fake_pack(text)

    monkeypatch.setattr("proseforge.application.work.retriever.NarrativeRetriever.build", fake_build)


async def _seed_novel(settings: Settings, *, rag_enabled: bool | None = None) -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"novelist-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            project = Project.create(owner_id=user.id, slug=f"novel-{uuid.uuid4().hex[:8]}", title="Novel")
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
            if rag_enabled is not None:
                uow.session.add(UserBuiltinSkillStateModel(
                    id=new_id(), user_id=user.id, skill_key=NARRATIVE_RAG_SKILL_KEY,
                    enabled=rag_enabled, created_at=datetime.now(UTC),
                ))
            chapter = Chapter.create(project_id=project.id, chapter_no=1, title="Opening")
            await uow.chapters.add(chapter)
            run = await uow.workflows.create(project.id, "novel", status="QUEUED")
            await uow.commit()
            return {"user_id": user.id, "project_id": project.id, "run_id": run.id}
    finally:
        await engine.dispose()


def _capture_loop(monkeypatch, captured: dict) -> None:
    async def fake_loop(provider, *, writer_model, editor_model, project_title, chapter_title, context_text,
                        usage_call_id_factory=None, on_usage=None, editor_provider=None, reviser_model=None, reviser_provider=None):
        captured["context_text"] = context_text
        return "chapter text", 0, {"status": "PASS"}

    monkeypatch.setattr("proseforge.workflows.novel_generation.run_writer_editor_loop", fake_loop)


@pytest.mark.asyncio
async def test_novel_switch_on_injects_scene_pack(settings_env, monkeypatch):
    seeded = await _seed_novel(settings_env)  # no row -> default ON
    calls: list = []
    _patch_retriever(monkeypatch, calls=calls)
    captured: dict = {}
    _capture_loop(monkeypatch, captured)

    result = await generate_novel({
        "workflow_id": seeded["run_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a",
    })

    assert result == "completed"
    assert len(calls) == 1 and calls[0]["chapter_no"] == 1
    assert captured["context_text"].startswith("[世界观与设定]\n[场景包] 测试场景")


@pytest.mark.asyncio
async def test_novel_switch_off_keeps_legacy_context(settings_env, monkeypatch):
    seeded = await _seed_novel(settings_env, rag_enabled=False)
    calls: list = []
    _patch_retriever(monkeypatch, calls=calls)
    captured: dict = {}
    _capture_loop(monkeypatch, captured)

    result = await generate_novel({
        "workflow_id": seeded["run_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a",
    })

    assert result == "completed"
    assert calls == []  # retriever never invoked
    assert "[场景包]" not in captured["context_text"]


@pytest.mark.asyncio
async def test_novel_retriever_failure_degrades_to_legacy(settings_env, monkeypatch):
    seeded = await _seed_novel(settings_env)
    _patch_retriever(monkeypatch, error=RuntimeError("pg down"))
    captured: dict = {}
    _capture_loop(monkeypatch, captured)

    result = await generate_novel({
        "workflow_id": seeded["run_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a",
    })

    assert result == "completed"  # never blocks writing
    assert "[场景包]" not in captured["context_text"]


class SpyProvider:
    provider_id = "openai"

    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield GenerationEvent("content.delta", "续写")


async def _seed_chat(
    settings: Settings,
    *,
    mode: str,
    with_credential: bool = True,
    with_indexed_chapter: bool = True,
    user_text: str = "继续写烛龙之战",
    target_status: str = "PENDING",
    context_window: int = 8192,
    max_output_tokens: int = 1024,
) -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"writer-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            project = Project.create(owner_id=user.id, slug=f"proj-{uuid.uuid4().hex[:8]}", title="Novel", mode=mode)
            await uow.projects.add(project)
            if with_credential:
                credential_id = f"cred-{uuid.uuid4().hex[:8]}"
                associated = f"{user.id}:openai:{credential_id}".encode()
                encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(
                    json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated
                )
                await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            await uow.model_catalog.upsert([
                ProviderModel("openai", "gpt-a", "GPT A", {}, context_window=context_window, max_output_tokens=max_output_tokens)
            ])
            if with_indexed_chapter:
                # One chapter with an active version makes the project's
                # retrieval index non-empty (generate_chat skips the pack
                # build entirely when nothing is indexable).
                chapter = Chapter.create(project_id=project.id, chapter_no=1, title="Opening")
                await uow.chapters.add(chapter)
                version = await uow.chapters.append_version(chapter_id=chapter.id, content="已采纳的正文")
                await uow.chapters.set_active_version(chapter.id, version.id)
            conversation = Conversation.create(project.id, "Chat")
            main = await uow.conversations.create(conversation)
            await uow.conversations.append_message(main.id, "user", user_text)
            target = await uow.conversations.append_message(main.id, "assistant", "", None, target_status)
            uow.session.add(ConversationModel(id=target.id, project_id=project.id, title="message-stream"))
            await uow.commit()
            return {"user_id": user.id, "project_id": project.id, "message_id": target.id, "conversation_id": conversation.id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_chat_work_mode_injects_pack_as_first_system_block(settings_env, monkeypatch):
    seeded = await _seed_chat(settings_env, mode="work")
    calls: list = []
    _patch_retriever(monkeypatch, calls=calls)
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a", "reasoning_level": "auto",
    })

    assert result == "completed"
    assert len(calls) == 1 and "烛龙之战" in calls[0]["query"]
    # Snapshot linkage: the run must be attributable to this message/conversation.
    assert calls[0]["message_id"] == seeded["message_id"]
    assert calls[0]["conversation_id"] == seeded["conversation_id"]
    assert any("[场景包] 测试场景" in str(block.get("text", "")) for block in spy.requests[0].system_blocks)


@pytest.mark.asyncio
async def test_chat_mode_calls_retriever_when_switch_on(settings_env, monkeypatch):
    # W4: chat projects share the narrative-RAG switch with work mode — the
    # default-ON switch builds and injects the pack for chat mode too.
    seeded = await _seed_chat(settings_env, mode="chat")
    calls: list = []
    _patch_retriever(monkeypatch, calls=calls)
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a", "reasoning_level": "auto",
    })

    assert result == "completed"
    assert len(calls) == 1 and "烛龙之战" in calls[0]["query"]
    assert calls[0]["message_id"] == seeded["message_id"]
    assert any("[场景包] 测试场景" in str(block.get("text", "")) for block in spy.requests[0].system_blocks)


@pytest.mark.asyncio
async def test_chat_retriever_failure_degrades(settings_env, monkeypatch):
    seeded = await _seed_chat(settings_env, mode="work")
    _patch_retriever(monkeypatch, error=RuntimeError("pg down"))
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a", "reasoning_level": "auto",
    })

    assert result == "completed"
    assert all("场景包" not in str(block.get("text", "")) for block in spy.requests[0].system_blocks)


@pytest.mark.asyncio
async def test_chat_missing_credential_fails_before_scene_pack(settings_env, monkeypatch):
    # L12c regression: the credential gate runs in the read-only pre-pass,
    # before the scene pack build -- a missing credential must not trigger a
    # retriever/embedding call or leave an orphan retrieval_runs row.
    seeded = await _seed_chat(settings_env, mode="work", with_credential=False)
    calls: list = []
    _patch_retriever(monkeypatch, calls=calls)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a", "reasoning_level": "auto",
    })

    assert result == "provider-not-configured"
    assert calls == []  # scene pack never built
    # The early-exit path still lands the terminal state + message.failed event.
    engine, factory = create_engine_and_sessionmaker(settings_env)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            message = await uow.conversations.get_message(seeded["message_id"])
            assert message is not None and message.status == "FAILED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_chat_unknown_message_exits_via_pre_pass(settings_env, monkeypatch):
    # L10a regression: a missing message exits through the simplified pre-pass
    # guard (message-only check) before any scene pack or credential work.
    seeded = await _seed_chat(settings_env, mode="work")
    calls: list = []
    _patch_retriever(monkeypatch, calls=calls)

    result = await generate_chat({
        "message_id": "missing-message-id", "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a", "reasoning_level": "auto",
    })

    assert result == "provider-not-configured"
    assert calls == []


@pytest.mark.asyncio
async def test_chat_empty_index_skips_retrieval(settings_env, monkeypatch):
    # No chapter has an active version -> retrieval provably returns
    # nothing: the pack build (embed spend + 0-hit retrieval_runs row) is
    # skipped, same guard as the agent executor.
    seeded = await _seed_chat(settings_env, mode="work", with_indexed_chapter=False)
    calls: list = []
    _patch_retriever(monkeypatch, calls=calls)
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a", "reasoning_level": "auto",
    })

    assert result == "completed"
    assert calls == []
    assert all("场景包" not in str(block.get("text", "")) for block in spy.requests[0].system_blocks)


@pytest.mark.asyncio
async def test_chat_cancelled_message_skips_pack_build(settings_env, monkeypatch):
    # A cancelled (or otherwise non-claimable) message never pays the
    # retrieval build; the claim transaction still arbitrates the race.
    seeded = await _seed_chat(settings_env, mode="work", target_status="CANCELLED")
    calls: list = []
    _patch_retriever(monkeypatch, calls=calls)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a", "reasoning_level": "auto",
    })

    assert result == "already-running"  # claim rejects CANCELLED
    assert calls == []


@pytest.mark.asyncio
async def test_chat_rag_query_capped_at_max_chars(settings_env, monkeypatch):
    # The raw user message is unbounded; the retrieval query sent to the
    # embedder is capped (API embedders bill/reject on the full text).
    seeded = await _seed_chat(settings_env, mode="work", user_text="问" * 5000)
    calls: list = []
    _patch_retriever(monkeypatch, calls=calls)
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a", "reasoning_level": "auto",
    })

    assert result == "completed"
    assert len(calls) == 1
    assert len(calls[0]["query"]) == QUERY_MAX_CHARS


@pytest.mark.asyncio
async def test_chat_scene_pack_trimmed_to_model_budget(settings_env, monkeypatch):
    # Small-window models hard-fail (provider 400) when system blocks
    # alone exceed the context window: the pack is trimmed to what the
    # input budget leaves after the system reserve before injection.
    # window 6000 / output 800 -> input 4600, reserve 4500 -> ~100 tokens.
    # The evidence section alone (5000 chars) blows that budget, so
    # trimming drops it while the tiny worldview section survives.
    seeded = await _seed_chat(settings_env, mode="work", context_window=6000, max_output_tokens=800)

    async def fake_build(self, **kwargs):
        sections = {"worldview": "[场景包] 设定", "current_state": "", "constraints": "", "evidence": "证" * 5000}
        return SimpleNamespace(text="[世界观与设定]\n[场景包] 设定", sections=sections, evidence=[], run_id="r1", token_cost=1)

    monkeypatch.setattr("proseforge.application.work.retriever.NarrativeRetriever.build", fake_build)
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a", "reasoning_level": "auto",
    })

    assert result == "completed"
    pack_blocks = [
        block for block in spy.requests[0].system_blocks
        if "场景包" in str(block.get("text", ""))
    ]
    assert len(pack_blocks) == 1  # worldview survives trimming
    assert "证" not in str(pack_blocks[0]["text"])  # over-budget evidence dropped
