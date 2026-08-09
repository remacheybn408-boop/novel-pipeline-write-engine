"""generate_novel 的 context 输入预算来自 catalog（V2-004 替换 input_budget=8000 硬编码）。

catalog context_window 经 calculate_budget 扣掉输出预留与安全边际后得到
input_budget；未知模型回落保守下限 8192，且解析结果（source）必须作为
workflow 事件落痕，绝不静默。
"""

from __future__ import annotations

import base64
import json
import os
import uuid

import pytest

from proseforge.domain.chapter.entity import Chapter
from proseforge.domain.ports.model_provider import ProviderModel
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings
from proseforge.workflows.tasks import generate_novel

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def novel_settings(tmp_path, monkeypatch):
    database_url = os.environ.get("PROSEFORGE_TEST_DATABASE_URL")
    profile = "test" if database_url else "native"
    if not database_url:
        database_url = f"sqlite+aiosqlite:///{(tmp_path / 'novel.db').as_posix()}"
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


async def _seed(settings: Settings, *, with_catalog: bool = True, context_chars: int = 3000) -> dict[str, str]:
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
            if with_catalog:
                await uow.model_catalog.upsert([
                    ProviderModel("openai", "gpt-small", "GPT Small", {}, context_window=2048, max_output_tokens=333)
                ])
            chapter = Chapter.create(project_id=project.id, chapter_no=1, title="Opening")
            await uow.chapters.add(chapter)
            run = await uow.workflows.create(project.id, "novel", status="QUEUED")
            # CJK 一字一 token：该条目的体量落在 catalog 预算与旧硬编码 8000 之间
            await uow.context.add(project.id, "manual", "雪" * context_chars, "backstory")
            await uow.commit()
            return {"user_id": user.id, "project_id": project.id, "run_id": run.id}
    finally:
        await engine.dispose()


def _capture_loop(monkeypatch, captured: dict) -> None:
    # Production also passes usage hooks (usage_call_id_factory/on_usage);
    # the stub accepts and ignores them.
    async def fake_loop(provider, *, writer_model, editor_model, project_title, chapter_title, context_text, usage_call_id_factory=None, on_usage=None, editor_provider=None, reviser_model=None, reviser_provider=None):
        captured["context_text"] = context_text
        return "chapter text", 0, {"status": "PASS"}

    monkeypatch.setattr("proseforge.workflows.novel_generation.run_writer_editor_loop", fake_loop)


async def _budget_events(settings: Settings, run_id: str) -> list[dict[str, object]]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            events = await uow.workflows.events(run_id)
            return [event["data"] for event in events if event["event"] == "context.budget"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_window_drives_context_input_budget(novel_settings, monkeypatch):
    seeded = await _seed(novel_settings)
    captured: dict[str, str] = {}
    _capture_loop(monkeypatch, captured)

    result = await generate_novel({
        "workflow_id": seeded["run_id"],
        "user_id": seeded["user_id"],
        "provider": "openai",
        "model": "gpt-small",
    })

    assert result == "completed"
    # catalog 2048 - 333 输出预留 - 204 安全边际 = 1511：3000-token 条目被裁（旧硬编码 8000 会保留）
    assert "雪" not in captured["context_text"]
    events = await _budget_events(novel_settings, seeded["run_id"])
    assert len(events) == 1
    assert events[0]["context_window"] == 2048
    assert events[0]["context_window_source"] == "catalog"
    assert events[0]["input_budget"] == 1511
    assert events[0]["output_reserve"] == 333


@pytest.mark.asyncio
async def test_unknown_model_falls_back_to_conservative_floor_and_records_source(novel_settings, monkeypatch):
    seeded = await _seed(novel_settings, with_catalog=False, context_chars=7000)
    captured: dict[str, str] = {}
    _capture_loop(monkeypatch, captured)

    result = await generate_novel({
        "workflow_id": seeded["run_id"],
        "user_id": seeded["user_id"],
        "provider": "openai",
        "model": "gpt-ghost",
    })

    assert result == "completed"  # 未知模型不得让工作流崩溃
    # floor 8192 - 1024 输出预留 - 819 安全边际 = 6349：7000-token 条目被裁（旧硬编码 8000 会保留）
    assert "雪" not in captured["context_text"]
    events = await _budget_events(novel_settings, seeded["run_id"])
    assert len(events) == 1
    assert events[0]["context_window"] == 8192
    assert events[0]["context_window_source"] == "fallback"
    assert events[0]["input_budget"] == 6349


@pytest.mark.asyncio
async def test_large_catalog_window_keeps_context_item(novel_settings, monkeypatch):
    seeded = await _seed(novel_settings, with_catalog=False, context_chars=3000)
    engine, factory = create_engine_and_sessionmaker(novel_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            await uow.model_catalog.upsert([
                ProviderModel("openai", "gpt-huge", "GPT Huge", {}, context_window=128000, max_output_tokens=4096)
            ])
            await uow.commit()
    finally:
        await engine.dispose()
    captured: dict[str, str] = {}
    _capture_loop(monkeypatch, captured)

    result = await generate_novel({
        "workflow_id": seeded["run_id"],
        "user_id": seeded["user_id"],
        "provider": "openai",
        "model": "gpt-huge",
    })

    assert result == "completed"
    assert "雪" * 100 in captured["context_text"]  # 大窗口下条目完整保留
    events = await _budget_events(novel_settings, seeded["run_id"])
    assert events[0]["context_window"] == 128000
    assert events[0]["context_window_source"] == "catalog"
