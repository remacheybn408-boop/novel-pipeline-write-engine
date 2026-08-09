"""Writing-progress aggregation + chapter download endpoints.

GET /api/v1/projects/{id}/writing-status folds agent_runs/agent_tasks/
chapters (plus the analyze run's batch plan) into per-chapter statuses;
GET .../chapters/download (zip) and .../chapters/{cid}/download (single)
export only completed chapters' active-version 正文.

Covers the three aggregate states (empty project / in-progress / completed),
the failed state, and the download contract (completed chapters only, body
is the 正文 alone). Real app on native sqlite (TestClient + lifespan); run
and task rows are forced directly because no API path lands a run in
RUNNING/FAILED with mid-pipeline task states.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from proseforge.api.main import create_app
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.agents import (
    AgentEventModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


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
        yield test_client


def _create_project(client: TestClient, slug: str = "novel-ws") -> str:
    response = client.post("/api/v1/projects", json={"slug": slug, "title": "Novel", "mode": "work"})
    assert response.status_code == 201
    return response.json()["id"]


def _create_chapter(client: TestClient, project_id: str, chapter_no: int, title: str, *, content: str | None = None) -> str:
    response = client.post(f"/api/v1/projects/{project_id}/chapters", json={"chapter_no": chapter_no, "title": title})
    assert response.status_code == 201, response.text
    chapter_id = response.json()["id"]
    if content is not None:
        response = client.post(f"/api/v1/chapters/{chapter_id}/versions", json={"content": content})
        assert response.status_code == 201, response.text
    return chapter_id


def _start_run(
    client: TestClient,
    project_id: str,
    *,
    goal: str,
    task_keys: list[str],
    idempotency_key: str | None = None,
) -> str:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    response = client.post(
        f"/api/v3/projects/{project_id}/agent-runs",
        json={"goal": goal, "tasks": [{"id": key, "role": "scene_writer"} for key in task_keys]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _force_run_state(client: TestClient, run_id: str, run_status: str, task_states: dict[str, str]) -> None:
    """Force the run row and per-task_key status directly (no API path
    lands a run mid-pipeline or terminal)."""

    async def _update() -> None:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run.status = run_status
            for task in await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id)):
                if task.task_key in task_states:
                    task.status = task_states[task.task_key]
            await uow.commit()

    asyncio.run(_update())


def _seed_batch_plan(client: TestClient, run_id: str, chapters: list[dict[str, object]]) -> None:
    """Append a batch.planned event to an analyze run (batch_dispatch's
    planning hook output)."""

    async def _insert() -> None:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run.status = "COMPLETED"
            sequence = int(run.event_cursor) + 1
            uow.session.add(AgentEventModel(
                id=new_id(), run_id=run_id, sequence=sequence, event_type="batch.planned",
                payload=json.dumps({"chapters": chapters, "total": len(chapters)}, ensure_ascii=False),
            ))
            run.event_cursor = sequence
            await uow.commit()

    asyncio.run(_insert())


def _chapter_map(payload: dict[str, object]) -> dict[int, dict[str, object]]:
    return {int(entry["chapter_no"]): entry for entry in payload["chapters"]}  # type: ignore[index, union-attr]


def _seed_promise_ledger(client: TestClient, project_id: str) -> None:
    """三条承诺台账：open / developing / resolved 各一。"""

    async def _insert() -> None:
        from datetime import UTC, datetime

        from proseforge.infrastructure.database.models.story_bible import (
            StoryBibleEntryModel,
        )

        now = datetime.now(UTC)
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            for status in ("open", "developing", "resolved"):
                uow.session.add(StoryBibleEntryModel(
                    id=new_id(), project_id=project_id, kind="promise", key=f"伏笔-{status}",
                    value_json="{}", status=status, confidence=1.0, source="promise_tracker",
                    pinned=False, version=1, created_at=now, updated_at=now,
                ))
            await uow.commit()

    asyncio.run(_insert())


def _seed_auto_pause_event(client: TestClient, run_id: str, payload: dict[str, object]) -> None:
    """Append a run.auto_paused event to a run (executor auto-pause output)."""

    async def _insert() -> None:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            sequence = int(run.event_cursor) + 1
            uow.session.add(AgentEventModel(
                id=new_id(), run_id=run_id, sequence=sequence, event_type="run.auto_paused",
                payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ))
            run.event_cursor = sequence
            await uow.commit()

    asyncio.run(_insert())


def test_writing_status_lanes(client: TestClient):
    """五条流水线状态栏：写作/审校/改写从章节聚合派生，动态承诺透传当前章
    run 的三节点状态，承诺台账给出 open/developing/resolved 计数。"""
    project_id = _create_project(client)
    analyze_run_id = _start_run(client, project_id, goal="全书大纲", task_keys=["analyze_structure", "analyze_cast", "analyze_hooks", "analyze_merge"])
    _seed_batch_plan(client, analyze_run_id, [
        {"chapter_no": 1, "title": "第一章", "summary": "", "hooks": ""},
        {"chapter_no": 2, "title": "第二章", "summary": "", "hooks": ""},
        {"chapter_no": 3, "title": "第三章", "summary": "", "hooks": ""},
    ])
    # Chapter 1: rewriting with promise nodes mid-flight (奥莉维亚在工作).
    run_1 = _start_run(
        client, project_id, goal="写第1章",
        task_keys=["scene_a", "rewrite", "promise_contract", "promise_verify", "promise_register"],
        idempotency_key=f"batch:{analyze_run_id}:1",
    )
    _force_run_state(client, run_1, "RUNNING", {
        "scene_a": "SUCCEEDED", "rewrite": "RUNNING",
        "promise_contract": "SUCCEEDED", "promise_verify": "RUNNING", "promise_register": "PENDING",
    })
    # Chapter 2: writing.
    run_2 = _start_run(client, project_id, goal="写第2章", task_keys=["scene_a"], idempotency_key=f"batch:{analyze_run_id}:2")
    _force_run_state(client, run_2, "RUNNING", {"scene_a": "RUNNING"})
    _seed_promise_ledger(client, project_id)

    response = client.get(f"/api/v1/projects/{project_id}/writing-status")

    assert response.status_code == 200, response.text
    lanes = response.json()["lanes"]
    assert lanes["writing"] == {"active": True, "chapter_no": 2, "detail": "场景起草中"}
    assert lanes["reviewing"] == {"active": False, "chapter_no": None, "detail": "空闲"}
    assert lanes["rewriting"] == {"active": True, "chapter_no": 1, "detail": "改写中"}
    pipeline = lanes["promise_pipeline"]
    assert pipeline["active"] is True
    assert pipeline["chapter_no"] == 1  # in-flight chapter's run wins
    assert (pipeline["contract"], pipeline["verify"], pipeline["register"]) == ("SUCCEEDED", "RUNNING", "PENDING")
    assert lanes["promise_ledger"] == {"open": 1, "developing": 1, "resolved": 1}


def test_writing_status_lanes_idle_project(client: TestClient):
    project_id = _create_project(client)

    response = client.get(f"/api/v1/projects/{project_id}/writing-status")

    lanes = response.json()["lanes"]
    assert lanes["writing"]["active"] is False
    assert lanes["promise_pipeline"] == {"active": False, "chapter_no": None, "contract": None, "verify": None, "register": None}
    assert lanes["promise_ledger"] == {"open": 0, "developing": 0, "resolved": 0}


def test_writing_status_empty_project(client: TestClient):
    project_id = _create_project(client)

    response = client.get(f"/api/v1/projects/{project_id}/writing-status")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total_chapters"] == 0
    assert payload["current_chapter_no"] is None
    assert payload["chapters"] == []


def test_writing_status_in_progress_aggregate(client: TestClient):
    project_id = _create_project(client)
    analyze_run_id = _start_run(client, project_id, goal="全书大纲", task_keys=["analyze"])
    plan = [
        {"chapter_no": number, "title": f"第{number}章 标题", "summary": "", "hooks": ""}
        for number in range(1, 6)
    ]
    _seed_batch_plan(client, analyze_run_id, plan)

    # Chapter 1: completed (COMPLETED run + active version written back).
    _create_chapter(client, project_id, 1, "第一章 雨夜", content="雨夜正文。")
    run_1 = _start_run(client, project_id, goal="写第1章《第一章 雨夜》", task_keys=["scene_a"])
    _force_run_state(client, run_1, "COMPLETED", {"scene_a": "SUCCEEDED"})

    # Chapter 2: drafting (scene task RUNNING), mapped via the batch key.
    run_2 = _start_run(
        client, project_id, goal="写第2章", task_keys=["scene_a", "scene_b"],
        idempotency_key=f"batch:{analyze_run_id}:2",
    )
    _force_run_state(client, run_2, "RUNNING", {"scene_b": "RUNNING"})

    # Chapter 3: reviewing (merge RUNNING); chapter 4: rewriting (recheck).
    run_3 = _start_run(client, project_id, goal="写第3章", task_keys=["scene_a", "merge"])
    _force_run_state(client, run_3, "RUNNING", {"scene_a": "SUCCEEDED", "merge": "RUNNING"})
    run_4 = _start_run(client, project_id, goal="写第4章", task_keys=["scene_a", "recheck"])
    _force_run_state(client, run_4, "RUNNING", {"scene_a": "SUCCEEDED", "recheck": "RUNNING"})
    # Chapter 5: no run yet -> not_started.

    response = client.get(f"/api/v1/projects/{project_id}/writing-status")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total_chapters"] == 5
    assert payload["current_chapter_no"] == 2
    chapters = _chapter_map(payload)
    assert chapters[1]["status"] == "completed"
    assert chapters[1]["downloadable"] is True
    assert chapters[1]["title"] == "第一章 雨夜"
    assert (chapters[2]["status"], chapters[2]["stage"]) == ("writing", "场景起草中")
    assert (chapters[3]["status"], chapters[3]["stage"]) == ("reviewing", "意见合并中")
    assert (chapters[4]["status"], chapters[4]["stage"]) == ("rewriting", "复核中")
    assert (chapters[5]["status"], chapters[5]["stage"]) == ("not_started", "未开始")
    assert chapters[5]["title"] == "第5章 标题"  # plan title fallback
    assert chapters[5]["downloadable"] is False


def test_writing_status_failed_run(client: TestClient):
    project_id = _create_project(client)
    _create_chapter(client, project_id, 1, "第一章", content=None)
    run_id = _start_run(client, project_id, goal="写第1章", task_keys=["scene_a"])
    _force_run_state(client, run_id, "FAILED", {"scene_a": "FAILED"})

    response = client.get(f"/api/v1/projects/{project_id}/writing-status")

    assert response.status_code == 200, response.text
    chapters = _chapter_map(response.json())
    assert (chapters[1]["status"], chapters[1]["stage"]) == ("failed", "写作失败")
    assert chapters[1]["downloadable"] is False
    assert response.json()["current_chapter_no"] is None


def test_writing_status_completed_requires_active_version(client: TestClient):
    # A COMPLETED run whose chapter never got an active version is still
    # mid-delivery, not completed.
    project_id = _create_project(client)
    _create_chapter(client, project_id, 1, "第一章", content=None)
    run_id = _start_run(client, project_id, goal="写第1章", task_keys=["scene_a"])
    _force_run_state(client, run_id, "COMPLETED", {"scene_a": "SUCCEEDED"})

    chapters = _chapter_map(client.get(f"/api/v1/projects/{project_id}/writing-status").json())

    assert chapters[1]["status"] == "writing"
    assert chapters[1]["downloadable"] is False


def test_writing_status_auto_pause_present(client: TestClient):
    """自动暂停的 run（PAUSED + run.auto_paused 事件）→ auto_pause 透传
    provider/model/error/run_id，前端据此渲染恢复条。"""
    project_id = _create_project(client)
    _create_chapter(client, project_id, 1, "第一章", content=None)
    run_id = _start_run(client, project_id, goal="写第1章", task_keys=["scene_a"])
    _force_run_state(client, run_id, "PAUSED", {"scene_a": "FAILED"})
    _seed_auto_pause_event(client, run_id, {"streak": 3, "provider": "openai", "model": "gpt-4.1-mini", "error": "HTTP 503"})

    response = client.get(f"/api/v1/projects/{project_id}/writing-status")

    assert response.status_code == 200, response.text
    assert response.json()["auto_pause"] == {
        "run_id": run_id,
        "reason": "HTTP 503",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "streak": 3,
    }


def test_writing_status_manual_pause_has_no_auto_pause(client: TestClient):
    """人工暂停（无 run.auto_paused 事件）→ auto_pause 为 null，不显示恢复条。"""
    project_id = _create_project(client)
    _create_chapter(client, project_id, 1, "第一章", content=None)
    run_id = _start_run(client, project_id, goal="写第1章", task_keys=["scene_a"])
    _force_run_state(client, run_id, "PAUSED", {"scene_a": "RUNNING"})

    response = client.get(f"/api/v1/projects/{project_id}/writing-status")

    assert response.status_code == 200, response.text
    assert response.json()["auto_pause"] is None


def test_writing_status_requires_work_project(client: TestClient):
    response = client.post("/api/v1/projects", json={"slug": "chat-proj", "title": "Chat", "mode": "chat"})
    assert response.status_code == 201
    chat_project_id = response.json()["id"]

    assert client.get(f"/api/v1/projects/{chat_project_id}/writing-status").status_code == 404
    assert client.get("/api/v1/projects/does-not-exist/writing-status").status_code == 404


def test_chapters_download_zip_completed_only(client: TestClient):
    project_id = _create_project(client)
    _create_chapter(client, project_id, 1, "第一章 雨夜", content="雨夜正文。")
    _create_chapter(client, project_id, 3, "第三章 真相", content="真相正文。")
    _create_chapter(client, project_id, 2, "第二章 途中", content=None)  # unfinished: skipped

    response = client.get(f"/api/v1/projects/{project_id}/chapters/download")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        members = sorted(archive.namelist())
        assert members == ["第1章_第一章 雨夜.md", "第3章_第三章 真相.md"]
        # Zip members carry the 正文 alone — no metadata wrapper.
        assert archive.read("第1章_第一章 雨夜.md").decode() == "雨夜正文。"
        assert archive.read("第3章_第三章 真相.md").decode() == "真相正文。"


def test_chapters_download_zip_empty_is_409(client: TestClient):
    project_id = _create_project(client)
    _create_chapter(client, project_id, 1, "第一章", content=None)

    assert client.get(f"/api/v1/projects/{project_id}/chapters/download").status_code == 409


def test_chapter_download_single(client: TestClient):
    project_id = _create_project(client)
    chapter_id = _create_chapter(client, project_id, 1, "第一章 雨夜", content="雨夜正文。")

    response = client.get(f"/api/v1/projects/{project_id}/chapters/{chapter_id}/download")

    assert response.status_code == 200, response.text
    assert response.text == "雨夜正文。"
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition and "UTF-8" in disposition


def test_chapter_download_unfinished_is_409(client: TestClient):
    project_id = _create_project(client)
    chapter_id = _create_chapter(client, project_id, 1, "第一章", content=None)

    assert client.get(f"/api/v1/projects/{project_id}/chapters/{chapter_id}/download").status_code == 409


def test_chapter_download_wrong_project_is_404(client: TestClient):
    project_id = _create_project(client)
    chapter_id = _create_chapter(client, project_id, 1, "第一章", content="正文。")
    other_project_id = _create_project(client, slug="novel-ws-2")

    assert client.get(f"/api/v1/projects/{other_project_id}/chapters/{chapter_id}/download").status_code == 404


# ---------------------------------------------------------------------------
# 流水线标签：评审合议 + 分析三席位节点
# ---------------------------------------------------------------------------


def test_stage_labels_for_council_and_analyze_seats():
    from proseforge.api.routes.writing_status import (
        _stage_for_task,
        derive_chapter_status,
    )

    assert _stage_for_task("review_council") == ("reviewing", "评审合议中")
    assert _stage_for_task("analyze_structure") == ("writing", "大纲拆解中")
    assert _stage_for_task("analyze_merge") == ("writing", "大纲融合中")
    # RUNNING 任务取流水线最靠后者：合议晚于三评审
    assert derive_chapter_status("RUNNING", ["review_continuity", "review_council"], has_active_version=False) == ("reviewing", "评审合议中")
