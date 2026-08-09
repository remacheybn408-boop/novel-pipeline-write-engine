"""Manual chapter writes must enqueue narrative-RAG indexing (M13 regression).

Covers the three manual routes that change what a chapter's active content
is: create chapter, append version (manual edit), and activate-version
(rollback). Indexing jobs are enqueued centrally by the set_active_version
funnel with get-or-create semantics: ONE pending index_chapter row per
chapter covers rapid successive writes (the worker reads the current active
version at run time), and every route still dispatches
``proseforge.retrieval.index_document`` after commit (queue replaced with a
recording stub; duplicate dispatches lose the worker's atomic claim_job).
Real app on native sqlite.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.infrastructure.database.models.retrieval import RetrievalJobModel
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def enqueue(self, task_name: str, payload: dict) -> str:
        self.calls.append((task_name, payload))
        return f"task-{len(self.calls)}"


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    with TestClient(create_app(settings)) as test_client:
        response = test_client.post("/api/v1/auth/setup", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 201
        response = test_client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 200
        test_client.app.state.queue = _RecordingQueue()
        yield test_client


def _index_jobs(client: TestClient, *, job_type: str | None = None) -> list[RetrievalJobModel]:
    async def _read() -> list[RetrievalJobModel]:
        from sqlalchemy import select

        async with client.app.state.session_factory() as session:
            jobs = list(await session.scalars(select(RetrievalJobModel).order_by(RetrievalJobModel.requested_at, RetrievalJobModel.id)))
            # Detach plain tuples: ORM instances expire with the session.
            rows = [(job.project_id, job.job_type, job.source_type, job.source_id, job.status, job.id) for job in jobs]
            # set_active_version now also enqueues summarize_chapter jobs
            # (recap invalidation); callers filter to the type under test.
            return [row for row in rows if job_type is None or row[1] == job_type]

    return asyncio.run(_read())


def _queue_calls(client: TestClient, *, task_name: str | None = None) -> list[tuple[str, dict]]:
    calls = list(client.app.state.queue.calls)
    return [call for call in calls if task_name is None or call[0] == task_name]


def _create_project(client: TestClient, slug: str = "proj-idx-1") -> str:
    response = client.post("/api/v1/projects", json={"slug": slug, "title": "Novel", "mode": "work"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_chapter(client: TestClient, project_id: str) -> str:
    response = client.post(f"/api/v1/projects/{project_id}/chapters", json={"chapter_no": 1, "title": "第一章 雨夜"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _append_version(client: TestClient, chapter_id: str, content: str) -> str:
    response = client.post(f"/api/v1/chapters/{chapter_id}/versions", json={"content": content})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _assert_one_index_call(client: TestClient, *, project_id: str, chapter_id: str) -> None:
    """Exactly one pending index_chapter job plus one matching worker dispatch."""
    jobs = _index_jobs(client)
    assert [(job[0], job[1], job[2], job[3], job[4]) for job in jobs] == [
        (project_id, "index_chapter", "chapter", chapter_id, "pending")
    ]
    calls = _queue_calls(client)
    assert len(calls) == 1
    task_name, payload = calls[0]
    assert task_name == "proseforge.retrieval.index_document"
    assert payload["job_id"] == jobs[0][5]
    assert payload["user_id"]
    assert set(payload) == {"job_id", "user_id"}


def test_create_chapter_enqueues_index(client: TestClient):
    project_id = _create_project(client)

    chapter_id = _create_chapter(client, project_id)

    _assert_one_index_call(client, project_id=project_id, chapter_id=chapter_id)


def test_append_version_enqueues_index(client: TestClient):
    project_id = _create_project(client)
    chapter_id = _create_chapter(client, project_id)
    client.app.state.queue.calls.clear()

    _append_version(client, chapter_id, "雨夜，主角回城。")

    jobs = _index_jobs(client, job_type="index_chapter")
    # Get-or-create: the create-time pending row is reused — it reads the
    # current active version at run time, so one row covers both writes.
    assert [(job[0], job[1], job[2], job[3], job[4]) for job in jobs] == [
        (project_id, "index_chapter", "chapter", chapter_id, "pending")
    ]
    calls = _queue_calls(client, task_name="proseforge.retrieval.index_document")
    assert len(calls) == 1
    task_name, payload = calls[0]
    assert task_name == "proseforge.retrieval.index_document"
    # The dispatch references the shared pending job row.
    assert payload["job_id"] == jobs[0][5]
    assert set(payload) == {"job_id", "user_id"}


def test_activate_version_rollback_enqueues_index(client: TestClient):
    project_id = _create_project(client)
    chapter_id = _create_chapter(client, project_id)
    first_version_id = _append_version(client, chapter_id, "第一版。")
    _append_version(client, chapter_id, "第二版。")
    client.app.state.queue.calls.clear()

    response = client.post(f"/api/v1/chapters/{chapter_id}/activate-version", params={"version_id": first_version_id})

    assert response.status_code == 200, response.text
    assert response.json()["active_version_id"] == first_version_id
    jobs = _index_jobs(client, job_type="index_chapter")
    # All content-changing calls ride the same pending index job; the worker
    # indexes whatever version is active when it runs.
    assert [job[1] for job in jobs] == ["index_chapter"]
    assert jobs[0][3] == chapter_id and jobs[0][4] == "pending"
    calls = _queue_calls(client, task_name="proseforge.retrieval.index_document")
    assert len(calls) == 1
    task_name, payload = calls[0]
    assert task_name == "proseforge.retrieval.index_document"
    assert payload["job_id"] == jobs[0][5]
    assert set(payload) == {"job_id", "user_id"}


def test_failed_append_version_conflict_enqueues_nothing(client: TestClient):
    project_id = _create_project(client)
    chapter_id = _create_chapter(client, project_id)
    _append_version(client, chapter_id, "第一版。")
    client.app.state.queue.calls.clear()

    response = client.post(f"/api/v1/chapters/{chapter_id}/versions", json={"content": "过期基线。", "base_version": 99})

    assert response.status_code == 409, response.text
    assert _queue_calls(client) == []
    # Still only the single shared index job: a rejected write enqueues nothing.
    assert len(_index_jobs(client, job_type="index_chapter")) == 1
