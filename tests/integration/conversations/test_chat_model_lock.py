"""Writing-model lock in generate_chat.

Work mode: a locked project overrides the requested provider/model;
reasoning level stays user-adjustable (resolved off the final model, never
part of the lock). Chat mode: lock fields are ignored entirely. Pattern
follows test_history_sent_to_provider.py (SpyProvider, no network).
"""

from __future__ import annotations

import base64
import json
import os
import uuid

import pytest

from proseforge.domain.conversation.entity import Conversation
from proseforge.domain.ports.model_provider import GenerationEvent, ProviderModel
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.conversation import ConversationModel
from proseforge.infrastructure.database.models.project import ProjectModel
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
        yield GenerationEvent("content.delta", "续写内容")


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


async def _seed(settings: Settings, *, mode: str = "work", lock: tuple[str, str] | None = None) -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"writer-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            project = Project.create(owner_id=user.id, slug=f"proj-{uuid.uuid4().hex[:8]}", title="Novel", mode=mode)
            await uow.projects.add(project)
            credential_id = f"cred-{uuid.uuid4().hex[:8]}"
            associated = f"{user.id}:openai:{credential_id}".encode()
            encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(
                json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated
            )
            await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            await uow.model_catalog.upsert([
                ProviderModel("openai", "gpt-locked", "GPT Locked", {"reasoning": True, "reasoning_parameter": "effort"}, context_window=8192, max_output_tokens=1024),
            ])
            if lock is not None:
                await uow.projects.lock_writing_model(project.id, provider=lock[0], model_id=lock[1], source="first_chapter")
            conversation = Conversation.create(project.id, "Chat")
            main = await uow.conversations.create(conversation)
            await uow.conversations.append_message(main.id, "user", "继续写第二章")
            target = await uow.conversations.append_message(main.id, "assistant", "", None, "PENDING")
            # message:{id} stream rows need a parent for the FK (see
            # test_history_sent_to_provider.py for the same workaround).
            uow.session.add(ConversationModel(id=target.id, project_id=project.id, title="message-stream"))
            await uow.commit()
            return {"user_id": user.id, "project_id": project.id, "message_id": target.id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_work_mode_locked_project_overrides_requested_model(chat_settings, monkeypatch):
    seeded = await _seed(chat_settings, mode="work", lock=("openai", "gpt-locked"))
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-requested", "reasoning_level": "auto",
    })

    assert result == "completed"
    assert len(spy.requests) == 1
    assert spy.requests[0].model == "gpt-locked"  # requested model ignored


@pytest.mark.asyncio
async def test_reasoning_level_not_part_of_lock(chat_settings, monkeypatch):
    seeded = await _seed(chat_settings, mode="work", lock=("openai", "gpt-locked"))
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-requested", "reasoning_level": "high",
    })

    assert result == "completed"
    request = spy.requests[0]
    assert request.model == "gpt-locked"
    # Reasoning stays user-adjustable and is resolved off the final (locked)
    # model's capabilities: the catalog reasoning_parameter passes through.
    assert request.reasoning is not None and "effort" in str(request.reasoning)


@pytest.mark.asyncio
async def test_chat_mode_ignores_lock_fields(chat_settings, monkeypatch):
    # Chat projects never lock; even with stray lock fields set, the
    # requested model must pass through untouched.
    seeded = await _seed(chat_settings, mode="chat", lock=("openai", "gpt-locked"))
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-requested", "reasoning_level": "auto",
    })

    assert result == "completed"
    assert spy.requests[0].model == "gpt-requested"


@pytest.mark.asyncio
async def test_work_mode_unlocked_uses_requested_model(chat_settings, monkeypatch):
    seeded = await _seed(chat_settings, mode="work", lock=None)
    spy = SpyProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: spy)

    result = await generate_chat({
        "message_id": seeded["message_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-requested", "reasoning_level": "auto",
    })

    assert result == "completed"
    assert spy.requests[0].model == "gpt-requested"
    # No lock may be written by chat generation (only outline import and
    # chapter versions lock).
    engine, factory = create_engine_and_sessionmaker(chat_settings)
    try:
        async with factory() as session:
            from sqlalchemy import select

            row = (await session.scalars(select(ProjectModel).where(ProjectModel.id == seeded["project_id"]))).one()
            assert row.model_locked_at is None
    finally:
        await engine.dispose()
