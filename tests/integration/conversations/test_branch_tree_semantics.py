from __future__ import annotations

import base64
import os
import uuid

import pytest

from proseforge.application.conversations.compare_branches import compare_messages
from proseforge.application.conversations.regenerate_reply import RegenerateReply
from proseforge.application.conversations.send_message import SendMessage
from proseforge.domain.conversation.entity import Conversation
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings, get_settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


class FakeQueue:
    def __init__(self):
        self.enqueued = []

    async def enqueue(self, task, payload):
        self.enqueued.append((task, payload))
        return f"task-{len(self.enqueued)}"


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    database_url = os.environ.get("PROSEFORGE_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{(tmp_path / 'branches.db').as_posix()}"
    profile = "test" if os.environ.get("PROSEFORGE_TEST_DATABASE_URL") else "native"
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


async def _seed(settings: Settings):
    engine, factory = create_engine_and_sessionmaker(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        user = await uow.users.create(f"writer-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
        project = Project.create(owner_id=user.id, slug=f"novel-{uuid.uuid4().hex[:8]}", title="Novel")
        await uow.projects.add(project)
        conversation = Conversation.create(project.id, "Chat")
        main = await uow.conversations.create(conversation)
        await uow.commit()
        ids = {"user_id": user.id, "project_id": project.id, "conversation_id": conversation.id, "branch_id": main.id}
    return engine, factory, ids


@pytest.mark.asyncio
async def test_send_links_assistant_to_user_message_and_regenerate_increments_attempts(settings):
    engine, factory, ids = await _seed(settings)
    try:
        queue = FakeQueue()
        user_message, assistant, _ = await SendMessage(lambda: SqlAlchemyUnitOfWork(factory), queue).execute(
            branch_id=ids["branch_id"], content="two takes", client_request_id=f"crid-{uuid.uuid4().hex[:12]}", user_id=ids["user_id"],
        )
        assert assistant.parent_message_id == user_message.id  # 候选分组的父边
        assert assistant.generation_attempt == 1

        use_case = RegenerateReply(lambda: SqlAlchemyUnitOfWork(factory), queue)
        first, _ = await use_case.execute(branch_id=ids["branch_id"], parent_message_id=assistant.parent_message_id, user_id=ids["user_id"], provider="openai", model="m", reasoning_level="auto")
        second, _ = await use_case.execute(branch_id=ids["branch_id"], parent_message_id=assistant.parent_message_id, user_id=ids["user_id"], provider="openai", model="m", reasoning_level="auto")

        async with SqlAlchemyUnitOfWork(factory) as uow:
            tree = await uow.conversations.list_visible_messages(ids["branch_id"])
        candidates = [message for message in tree if message.role == "assistant"]
        assert [message.generation_attempt for message in candidates] == [1, 2, 3]
        assert {message.parent_message_id for message in candidates} == {user_message.id}
        assert first.generation_attempt == 2 and second.generation_attempt == 3
        # 原候选保留：regenerate 不 fork、不覆盖
        assert {message.id for message in candidates} == {assistant.id, first.id, second.id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_assembly_keeps_only_the_latest_regenerate_candidate(settings):
    """M9 回归：regenerate 两次后，上下文组装路径只含最新候选，旧候选被过滤。"""
    from proseforge.domain.conversation.candidates import latest_attempt_per_group

    engine, factory, ids = await _seed(settings)
    try:
        queue = FakeQueue()
        user_message, assistant, _ = await SendMessage(lambda: SqlAlchemyUnitOfWork(factory), queue).execute(
            branch_id=ids["branch_id"], content="two takes", client_request_id=f"crid-{uuid.uuid4().hex[:12]}", user_id=ids["user_id"],
        )
        use_case = RegenerateReply(lambda: SqlAlchemyUnitOfWork(factory), queue)
        await use_case.execute(branch_id=ids["branch_id"], parent_message_id=assistant.parent_message_id, user_id=ids["user_id"], provider="openai", model="m", reasoning_level="auto")
        second, _ = await use_case.execute(branch_id=ids["branch_id"], parent_message_id=assistant.parent_message_id, user_id=ids["user_id"], provider="openai", model="m", reasoning_level="auto")

        async with SqlAlchemyUnitOfWork(factory) as uow:
            visible = await uow.conversations.list_visible_messages(ids["branch_id"])
        # 展示路径不过滤：三个候选都在。
        assert len([message for message in visible if message.role == "assistant"]) == 3

        context_history = latest_attempt_per_group(visible)
        assert [message.id for message in context_history] == [user_message.id, second.id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_branches_excludes_archived_by_default(settings):
    engine, factory, ids = await _seed(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user_message = await uow.conversations.append_message(ids["branch_id"], "user", "fork point")
            fork = await uow.conversations.fork_owned(ids["conversation_id"], user_message.id, "Edited", ids["user_id"])
            await uow.commit()
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert await uow.conversations.archive_branch(fork.id, ids["conversation_id"], ids["user_id"]) is True
            await uow.commit()

        async with SqlAlchemyUnitOfWork(factory) as uow:
            default = await uow.conversations.list_branches(ids["conversation_id"], ids["user_id"])
            full = await uow.conversations.list_branches(ids["conversation_id"], ids["user_id"], include_archived=True)
        assert {branch.id for branch in default} == {ids["branch_id"]}  # 归档默认隐藏
        assert {branch.id for branch in full} == {ids["branch_id"], fork.id}
        archived = next(branch for branch in full if branch.id == fork.id)
        assert archived.status == "ARCHIVED"
        # 归档只是状态翻转，历史仍然可读
        async with SqlAlchemyUnitOfWork(factory) as uow:
            tree = await uow.conversations.list_visible_messages(fork.id)
        assert [message.content for message in tree] == ["fork point"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_compare_returns_common_prefix_and_message_level_tails(settings):
    engine, factory, ids = await _seed(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user_message = await uow.conversations.append_message(ids["branch_id"], "user", "original")
            assistant = await uow.conversations.append_message(ids["branch_id"], "assistant", "reply", parent_message_id=user_message.id)
            fork = await uow.conversations.fork_owned(ids["conversation_id"], user_message.id, "Edited", ids["user_id"])
            replacement = await uow.conversations.append_message(fork.id, "user", "edited", parent_message_id=user_message.id)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(factory) as uow:
            left = await uow.conversations.list_visible_messages(ids["branch_id"])
            right = await uow.conversations.list_visible_messages(fork.id)
        result = compare_messages(left, right)
        assert result["common_count"] == 1  # fork 点之前的共同前缀
        assert [entry["id"] for entry in result["left"]] == [assistant.id]
        assert result["right"] == [{"id": replacement.id, "role": "user", "content": "edited", "generation_attempt": 1, "parent_message_id": user_message.id}]
        # 原分支消息流不被比较或 fork 修改
        assert [message.content for message in left] == ["original", "reply"]
    finally:
        await engine.dispose()
