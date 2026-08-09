"""Writing-model lock in generate_novel (work-mode chapter generation).

Covers: unlocked project locks on the first generated chapter version
(source "first_chapter"); a locked project ignores the requested model; cluster
mode splits writer/editor/reviser across configured models. The writer/
editor loop is stubbed — no network. Pattern follows
test_generate_novel_context_budget.py.
"""

from __future__ import annotations

import base64
import json
import os
import uuid

import pytest

from proseforge.application.models.cluster_config import CLUSTER_PREF_KEY
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


def _credential_payload(user_id: str, provider: str, credential_id: str) -> str:
    associated = f"{user_id}:{provider}:{credential_id}".encode()
    encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(
        json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated
    )
    return base64.b64encode(encrypted).decode()


async def _seed(settings: Settings, *, lock: tuple[str, str] | None = None, cluster: dict | None = None) -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"novelist-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            project = Project.create(owner_id=user.id, slug=f"novel-{uuid.uuid4().hex[:8]}", title="Novel")
            await uow.projects.add(project)
            for provider in ("openai", "deepseek"):
                credential_id = f"cred-{provider}-{uuid.uuid4().hex[:6]}"
                await uow.credentials.create(user.id, provider, _credential_payload(user.id, provider, credential_id), record_id=credential_id)
            await uow.model_catalog.upsert([
                ProviderModel("openai", "gpt-a", "GPT A", {}, context_window=8192, max_output_tokens=1024),
                ProviderModel("openai", "gpt-b", "GPT B", {}, context_window=8192, max_output_tokens=1024),
                ProviderModel("deepseek", "deepseek-chat", "DS Chat", {}, context_window=8192, max_output_tokens=1024),
            ])
            if lock is not None:
                await uow.projects.lock_writing_model(project.id, provider=lock[0], model_id=lock[1], source="outline_import")
            if cluster is not None:
                await uow.user_preferences.set(user.id, CLUSTER_PREF_KEY, json.dumps(cluster))
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
        captured["writer_model"] = writer_model
        captured["editor_model"] = editor_model
        captured["reviser_model"] = reviser_model
        captured["writer_provider"] = getattr(provider, "provider_id", None)
        captured["editor_provider"] = getattr(editor_provider, "provider_id", None) if editor_provider else None
        captured["reviser_provider"] = getattr(reviser_provider, "provider_id", None) if reviser_provider else None
        return "chapter text", 0, {"status": "PASS"}

    monkeypatch.setattr("proseforge.workflows.novel_generation.run_writer_editor_loop", fake_loop)


async def _project_lock(settings: Settings, project_id: str) -> dict[str, object]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with factory() as session:
            from sqlalchemy import select

            from proseforge.infrastructure.database.models.project import ProjectModel

            row = (await session.scalars(select(ProjectModel).where(ProjectModel.id == project_id))).one()
            return {
                "provider": row.writing_model_provider,
                "model": row.writing_model_id,
                "locked": row.model_locked_at is not None,
                "source": row.model_lock_source,
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_first_chapter_version_locks_writing_model(novel_settings, monkeypatch):
    seeded = await _seed(novel_settings)
    captured: dict = {}
    _capture_loop(monkeypatch, captured)

    result = await generate_novel({
        "workflow_id": seeded["run_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-a",
    })

    assert result == "completed"
    assert captured["writer_model"] == "gpt-a"
    lock = await _project_lock(novel_settings, seeded["project_id"])
    assert lock == {"provider": "openai", "model": "gpt-a", "locked": True, "source": "first_chapter"}


@pytest.mark.asyncio
async def test_locked_project_overrides_requested_model(novel_settings, monkeypatch):
    seeded = await _seed(novel_settings, lock=("openai", "gpt-a"))
    captured: dict = {}
    _capture_loop(monkeypatch, captured)

    result = await generate_novel({
        "workflow_id": seeded["run_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-b",  # requested model must be ignored
    })

    assert result == "completed"
    assert captured["writer_model"] == "gpt-a"
    # Normal mode: review/revise follow the locked model too.
    assert captured["editor_model"] == "gpt-a"
    assert captured["reviser_model"] == "gpt-a"
    # Lock unchanged (first-come-first-served, outline source preserved).
    lock = await _project_lock(novel_settings, seeded["project_id"])
    assert lock["model"] == "gpt-a" and lock["source"] == "outline_import"


@pytest.mark.asyncio
async def test_cluster_mode_splits_roles(novel_settings, monkeypatch):
    cluster = {"mode": "cluster", "write_model": "openai/gpt-a", "review_model": "deepseek/deepseek-chat", "revise_model": None}
    seeded = await _seed(novel_settings, lock=("openai", "gpt-a"), cluster=cluster)
    captured: dict = {}
    _capture_loop(monkeypatch, captured)

    result = await generate_novel({
        "workflow_id": seeded["run_id"], "user_id": seeded["user_id"],
        "provider": "openai", "model": "gpt-b",
    })

    assert result == "completed"
    assert captured["writer_model"] == "gpt-a"
    assert captured["editor_model"] == "deepseek-chat"
    # revise null -> auto non-writer backup (deepseek-chat, the only other).
    assert captured["reviser_model"] == "deepseek-chat"
    assert captured["editor_provider"] == "deepseek"
    assert captured["reviser_provider"] is None  # same provider as review -> falls through
