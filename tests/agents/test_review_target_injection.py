"""H3/M5 回归测试：独立 review/revise 意图的章节目标注入与空转拒绝。

sqlite+aiosqlite 真实落库 + 假 provider（无网络、无 PG），种子模式复制自
tests/agents/test_review_handlers.py（WS-D）。覆盖：
- 独立 review 意图（depends_on=[]）按 goal 章节号注入章节正文，评审 prompt
  真正读到内容，评审行引用共享的 chapter-target artifact；
- 无法确定评审对象（章节不存在、goal 无章节号）时 review 任务 FAILED 且带中文
  可读原因，不再空转产空报告；revise 章节缺失时降级为新建章节草稿（B3）；
- M5：write 管线评审电池（depends_on 含 select）只审 select 择优产出，落选
  草稿不进评审 prompt；
- 独立 revise 意图注入章节正文改写；章节缺失时 rewrite 降级产新建章节草稿。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from proseforge.application.agents.review_target import parse_chapter_no
from proseforge.domain.chapter.entity import Chapter
from proseforge.domain.common.ids import new_id
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentReviewModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings
from proseforge.workflows.agent_executor import execute_run

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
CHAPTER_CONTENT = "雨夜，主角提着青铜钥匙回城。"


@pytest.fixture()
def executor_settings(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'agents.db').as_posix()}"
    monkeypatch.setenv("PROSEFORGE_DATABASE_URL", database_url)
    monkeypatch.setenv("PROSEFORGE_RUNTIME_PROFILE", "native")
    monkeypatch.setenv("PROSEFORGE_MASTER_KEY", MASTER_KEY)
    get_settings.cache_clear()
    yield Settings(
        database_url=database_url,
        runtime_profile="native",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    get_settings.cache_clear()


class RecordingProvider:
    """按 task_key 定制输出的假 provider，同时记录每次请求的完整 user prompt。"""

    provider_id = "fake"

    def __init__(self, payloads: dict[str, object] | None = None, usage: tuple[int, int] = (4, 2)):
        self._payloads = payloads or {}
        self._input, self._output = usage
        self.requests: list[dict[str, str]] = []

    async def stream(self, request):
        user_prompt = "\n".join(str(block.get("text", "")) for block in request.input_blocks)
        self.requests.append({"task_key": str(request.metadata.get("task_key", "")), "user_prompt": user_prompt})
        await asyncio.sleep(0)
        payload = self._payloads.get(str(request.metadata.get("task_key", "")), {"summary": "ok"})
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        yield GenerationEvent("response.started")
        yield GenerationEvent("content.delta", text=text)
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": self._input, "output_tokens": self._output, "total_tokens": self._input + self._output}})

    async def list_models(self):
        return []

    async def validate_credentials(self):
        return {"valid": True}

    async def count_tokens(self, request):
        return 1


def _patch_provider(monkeypatch, provider: RecordingProvider) -> None:
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)


async def _seed_run(
    settings: Settings,
    tasks: list[dict[str, object]],
    *,
    goal: str | None = None,
    chapters: list[tuple[int, str, str]] | None = None,
    budget_limit: int = 1000,
) -> dict[str, object]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"agents-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            credential_id = f"cred-{uuid.uuid4().hex[:8]}"
            associated = f"{user.id}:openai:{credential_id}".encode()
            encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated)
            await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            # mode="chat"：关闭叙事 RAG 场景包（避免检索意图调用污染 provider 请求记录）
            project = await uow.projects.add(Project.create(owner_id=user.id, slug=f"p-{uuid.uuid4().hex[:8]}", title="Target Test", mode="chat"))
            await uow.session.flush()  # FK parent: chapters -> projects
            chapter_ids: dict[int, str] = {}
            for chapter_no, title, content in chapters or []:
                chapter = await uow.chapters.add(Chapter.create(project_id=project.id, chapter_no=chapter_no, title=title))
                version = await uow.chapters.append_version(chapter_id=chapter.id, content=content)
                await uow.chapters.set_active_version(chapter.id, version.id)
                chapter_ids[chapter_no] = chapter.id
            now = datetime.now(UTC)
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id=project.id, goal=goal,
                goal_hash=hashlib.sha256((goal or "").encode()).hexdigest(),
                graph_revision=1, status="PENDING", budget_limit=budget_limit,
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()  # FK parent: agent_tasks -> agent_runs
            for item in tasks:
                uow.session.add(AgentTaskModel(
                    id=new_id(), run_id=run.id, task_key=str(item["id"]), role=str(item["role"]),
                    status="PENDING", token_budget=int(item.get("token_budget", 1)),
                    depends_on=json.dumps(item.get("depends_on", [])),
                ))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id, "project_id": project.id, "chapter_ids": chapter_ids}
    finally:
        await engine.dispose()


async def _read_state(settings: Settings, run_id: str):
    # 只读事务退出时 __aexit__ 会 rollback 并过期实例——必须在会话内快照为 dict
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run_snapshot = {key: getattr(run, key) for key in ("id", "status", "terminal_reason")}
            tasks = [
                {key: getattr(task, key) for key in ("task_key", "role", "status", "attempts", "last_error")}
                for task in await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id).order_by(AgentTaskModel.id))
            ]
            artifacts = [
                {key: getattr(artifact, key) for key in ("id", "task_id", "artifact_type", "payload")}
                for artifact in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run_id))
            ]
            reviews = [
                {key: getattr(review, key) for key in ("id", "artifact_id", "reviewer_role", "status", "evidence")}
                for review in await uow.session.scalars(select(AgentReviewModel).where(AgentReviewModel.run_id == run_id))
            ]
            return run_snapshot, tasks, artifacts, reviews
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# parse_chapter_no 纯函数
# ---------------------------------------------------------------------------


def test_parse_chapter_no_digits_and_chinese_numerals():
    assert parse_chapter_no("审校第三章") == 3
    assert parse_chapter_no("改写第3章") == 3
    assert parse_chapter_no("请评审 第十二章 的内容") == 12
    assert parse_chapter_no("第二十三章") == 23
    assert parse_chapter_no("检查一下") is None
    assert parse_chapter_no("") is None


# ---------------------------------------------------------------------------
# H3：独立 review 意图（depends_on=[]）注入章节正文
# ---------------------------------------------------------------------------

_REVIEW_GRAPH = [
    {"id": "review_continuity", "role": "continuity_reviewer", "depends_on": []},
    {"id": "review_adversarial", "role": "adversarial_reviewer", "depends_on": []},
    {"id": "review_style", "role": "style_editor", "depends_on": []},
]


@pytest.mark.asyncio
async def test_standalone_review_reads_chapter_full_text(executor_settings, monkeypatch):
    review_payload = {
        "summary": "评审完成",
        "findings": [{"finding": "钥匙来路不明", "severity": "high", "evidence_spans": [{"artifact_id": "", "start": 0, "end": 4, "quote": "青铜钥匙"}], "verdict": "WARNING"}],
    }
    provider = RecordingProvider(payloads={key: review_payload for key in ("review_continuity", "review_adversarial", "review_style")})
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(
        executor_settings,
        _REVIEW_GRAPH,
        goal="审校第三章",
        chapters=[(3, "第三章", CHAPTER_CONTENT)],
    )

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, artifacts, reviews = await _read_state(executor_settings, str(seeded["run_id"]))
    assert run["status"] == "COMPLETED"
    assert all(task["status"] == "SUCCEEDED" for task in tasks)
    # 章节正文持久化为共享的 chapter-target artifact（三个并行评审共用一行）
    target_id = f"chapter-target-{seeded['run_id']}"
    target = next((artifact for artifact in artifacts if artifact["id"] == target_id), None)
    assert target is not None
    target_payload = json.loads(target["payload"])
    assert target_payload["content"] == CHAPTER_CONTENT
    assert target_payload["source_chapter_id"] == seeded["chapter_ids"][3]
    # 每个评审一行，全部引用 chapter-target artifact，且 verdict 带真实证据
    assert len(reviews) == 3
    assert {review["artifact_id"] for review in reviews} == {target_id}
    assert all(review["status"] == "WARNING" for review in reviews)
    # 评审 prompt 真正读到了章节正文（不再是 "reviewed 0 artifacts" 空转）
    assert len(provider.requests) == 3
    assert all(CHAPTER_CONTENT in request["user_prompt"] for request in provider.requests)


@pytest.mark.asyncio
async def test_standalone_review_missing_chapter_fails_loudly(executor_settings, monkeypatch):
    provider = RecordingProvider()
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, _REVIEW_GRAPH, goal="审校第三章")  # 项目里没有任何章节

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "failed"
    run, tasks, artifacts, reviews = await _read_state(executor_settings, str(seeded["run_id"]))
    assert run["status"] == "FAILED"
    assert all(task["status"] == "FAILED" for task in tasks)
    assert all("第3章不存在" in (task["last_error"] or "") for task in tasks)
    assert provider.requests == []  # 无评审对象：一次模型调用都不该发生
    assert artifacts == []  # 不再产出 "reviewed 0 artifacts" 的空报告
    assert reviews == []


@pytest.mark.asyncio
async def test_standalone_review_without_chapter_reference_fails_loudly(executor_settings, monkeypatch):
    provider = RecordingProvider()
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(
        executor_settings,
        _REVIEW_GRAPH,
        goal="帮忙审校一下",
        chapters=[(3, "第三章", CHAPTER_CONTENT)],  # 有章节但请求没指明哪一章
    )

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "failed"
    _run, tasks, artifacts, reviews = await _read_state(executor_settings, str(seeded["run_id"]))
    assert all(task["status"] == "FAILED" for task in tasks)
    assert all("无法确定评审对象" in (task["last_error"] or "") for task in tasks)
    assert provider.requests == []
    assert artifacts == []
    assert reviews == []


# ---------------------------------------------------------------------------
# M5：write 管线评审电池只审 select 择优产出
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_battery_audits_only_selected_winner(executor_settings, monkeypatch):
    provider = RecordingProvider(payloads={
        "scene_a": {"title": "甲稿", "content": "短稿甲" * 10},
        "scene_b": {"title": "乙稿", "content": "长稿乙" * 100},
        "scene_c": {"title": "丙稿", "content": "中稿丙" * 50},
        "review_continuity": {"summary": "评审完成", "findings": []},
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [
        {"id": "scene_a", "role": "scene_writer", "depends_on": []},
        {"id": "scene_b", "role": "scene_writer", "depends_on": []},
        {"id": "scene_c", "role": "scene_writer", "depends_on": []},
        {"id": "select", "role": "merge_editor", "depends_on": ["scene_a", "scene_b", "scene_c"]},
        {"id": "review_continuity", "role": "continuity_reviewer", "depends_on": ["select"]},
    ])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, _tasks, artifacts, reviews = await _read_state(executor_settings, str(seeded["run_id"]))
    assert run["status"] == "COMPLETED"
    select_artifact = next(artifact for artifact in artifacts if json.loads(artifact["payload"]).get("selected_from"))
    # 只有一行评审，且目标是 select 的择优产出（落选草稿不送审）
    assert len(reviews) == 1
    assert reviews[0]["artifact_id"] == select_artifact["id"]
    review_requests = [request for request in provider.requests if request["task_key"] == "review_continuity"]
    assert len(review_requests) == 1
    assert "长稿乙" in review_requests[0]["user_prompt"]
    assert "短稿甲" not in review_requests[0]["user_prompt"]
    assert "中稿丙" not in review_requests[0]["user_prompt"]


# ---------------------------------------------------------------------------
# H3：独立 revise 意图（merge -> rewrite -> recheck）注入章节正文改写
# ---------------------------------------------------------------------------

_REVISE_GRAPH = [
    {"id": "merge", "role": "merge_editor", "depends_on": []},
    {"id": "rewrite", "role": "chief_editor", "depends_on": ["merge"]},
    {"id": "recheck", "role": "continuity_reviewer", "depends_on": ["rewrite"]},
]


@pytest.mark.asyncio
async def test_standalone_revise_rewrites_chapter_content(executor_settings, monkeypatch):
    provider = RecordingProvider(payloads={
        "rewrite": {"title": "第三章（终稿）", "content": "改写后的正文：雨夜，主角提着青铜钥匙回城，城门已闭。"},
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(
        executor_settings,
        _REVISE_GRAPH,
        goal="改写第三章",
        chapters=[(3, "第三章", CHAPTER_CONTENT)],
    )

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, artifacts, _reviews = await _read_state(executor_settings, str(seeded["run_id"]))
    assert run["status"] == "COMPLETED"
    assert {task["task_key"]: task["status"] for task in tasks} == {"merge": "SUCCEEDED", "rewrite": "SUCCEEDED", "recheck": "SUCCEEDED"}
    rewritten = next(payload for payload in (json.loads(artifact["payload"]) for artifact in artifacts) if payload.get("rewrite_of"))
    assert rewritten["content"] == "改写后的正文：雨夜，主角提着青铜钥匙回城，城门已闭。"
    # 第一轮改写对象指回章节（第二轮基于第一轮终稿继续改，rewrite_of 指回第一轮 artifact）
    assert any(json.loads(artifact["payload"]).get("rewrite_of") == seeded["chapter_ids"][3] for artifact in artifacts)
    # 改写 prompt 真正读到了章节正文（26 字假稿低于默认 2500 字篇幅硬要求，
    # 每轮触发一轮扩写重试；终稿门禁 FAIL 后再走第二轮，共 2 轮 × 2 次调用）。
    # 第一轮两轮 prompt 都必须携带章节正文；第二轮的改写基底是第一轮终稿。
    rewrite_requests = [request for request in provider.requests if request["task_key"] == "rewrite"]
    assert len(rewrite_requests) == 4
    for request in rewrite_requests[:2]:
        assert CHAPTER_CONTENT in request["user_prompt"]
    for request in rewrite_requests[2:]:
        assert "改写后的正文" in request["user_prompt"]


@pytest.mark.asyncio
async def test_standalone_revise_missing_chapter_degrades_to_new_chapter_draft(executor_settings, monkeypatch):
    """B3：章节不存在时独立 revise 不再 FAILED——降级为新建章节草稿。"""
    provider = RecordingProvider(payloads={
        "rewrite": {"title": "第三章（新建）", "content": "新撰写的章节正文：雨夜，主角提着青铜钥匙回城。"},
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, _REVISE_GRAPH, goal="改写第三章")  # 项目里没有任何章节

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, artifacts, _reviews = await _read_state(executor_settings, str(seeded["run_id"]))
    assert run["status"] == "COMPLETED"
    status_by_key = {task["task_key"]: task["status"] for task in tasks}
    assert status_by_key == {"merge": "SUCCEEDED", "rewrite": "SUCCEEDED", "recheck": "SUCCEEDED"}
    # 降级产出为新建章节草稿：new_chapter 标记 + 中文说明，不带 rewrite_of
    drafted = next(payload for payload in (json.loads(artifact["payload"]) for artifact in artifacts) if payload.get("new_chapter"))
    assert drafted["content"] == "新撰写的章节正文：雨夜，主角提着青铜钥匙回城。"
    assert drafted["new_chapter"] is True
    assert "新建" in drafted["note"]
    assert "rewrite_of" not in drafted
