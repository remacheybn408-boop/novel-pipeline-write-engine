"""WS-D chief_editor handler（chief_handler.py）与 chief-proposal 端点的宿主可跑测试。

sqlite+aiosqlite 真实落库 + FakeProvider 假模型（无网络、无 PG），
settings/credential 种子模式复制自 tests/agents/test_agent_executor.py（WS-A）。
覆盖：MergeCandidate + V2 RevisionProposal（approval-bound）、guard 阻塞（未裁定冲突
→ guard_status=blocked → V2 approve 抛 ApprovalBlocked/422 语义）、无章节目标时只产
MergeCandidate、模型不可用回退候选摘要、POST .../chief-proposal 端点契约。
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from proseforge.domain.chapter.entity import Chapter
from proseforge.domain.common.ids import new_id
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentEventModel,
    AgentPolicySnapshotModel,
    AgentReviewModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.revision import RevisionProposalModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings
from proseforge.workflows.agent_executor import execute_run

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
BASE_CONTENT = "正文：雨夜，主角回城。"


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


class FakeProvider:
    """按 task_key 定制输出的假 provider（复制自 WS-A test_agent_executor.py）。"""

    provider_id = "fake"

    def __init__(self, payloads: dict[str, object] | None = None, usage: tuple[int, int] = (4, 2)):
        self._payloads = payloads or {}
        self._input, self._output = usage
        self.requests: list[dict[str, str]] = []

    async def stream(self, request):
        self.requests.append(dict(request.metadata))
        payload = self._payloads.get(request.metadata.get("task_key", ""), {"summary": "ok"})
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


def _patch_provider(monkeypatch, provider: FakeProvider) -> None:
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)


async def _seed_run(
    settings: Settings,
    tasks: list[dict[str, object]],
    *,
    status: str = "PENDING",
    with_chapter: bool = True,
    with_snapshot: bool = False,
    reviews: list[dict[str, object]] | None = None,
    budget_limit: int = 1000,
    goal: str | None = None,
) -> dict[str, str]:
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
            chapter_id = base_version_id = ""
            project_id = "project-1"
            if with_chapter:
                project = await uow.projects.add(Project.create(owner_id=user.id, slug=f"p-{uuid.uuid4().hex[:8]}", title="Chief"))
                project_id = project.id
                chapter = await uow.chapters.add(Chapter.create(project_id=project.id, chapter_no=1, title="第一章"))
                version = await uow.chapters.append_version(chapter_id=chapter.id, content=BASE_CONTENT)
                await uow.chapters.set_active_version(chapter.id, version.id)  # approve 的 base 校验走 active_version_id
                chapter_id, base_version_id = chapter.id, version.id
            else:
                # FK parent: agent_runs.project_id -> projects.id（无章节时仓库不会代建项目行）
                uow.session.add(ProjectModel(id=project_id, owner_id=user.id, slug=project_id, title="Chief Test Project"))
                await uow.session.flush()
            now = datetime.now(UTC)
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id=project_id,
                chapter_id=chapter_id or None, base_version_id=base_version_id or None,
                goal_hash="g" * 64, goal=goal, graph_revision=1, status=status, budget_limit=budget_limit,
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            # Run row must hit the DB before its agent_* children: relationship-less
            # tables flush in arbitrary order at commit, which breaks the new FKs.
            await uow.session.flush()
            for item in tasks:
                uow.session.add(AgentTaskModel(
                    id=new_id(), run_id=run.id, task_key=str(item["id"]), role=str(item["role"]),
                    status="PENDING", token_budget=int(item.get("token_budget", 1)),
                    depends_on=json.dumps(item.get("depends_on", [])),
                ))
            for item in reviews or []:
                uow.session.add(AgentReviewModel(
                    id=str(item["id"]), run_id=run.id, artifact_id=str(item.get("artifact_id", "art-1")),
                    reviewer_role=str(item.get("reviewer_role", "continuity_reviewer")), status=str(item["status"]),
                    evidence=json.dumps(item.get("evidence", []), ensure_ascii=False),
                    conflict_group=item.get("conflict_group"),
                    payload=json.dumps(item.get("payload", {}), ensure_ascii=False),
                ))
            if with_snapshot:
                from proseforge.application.agents.policy_snapshot import (
                    build_snapshot,
                    canonical_json,
                    sign,
                )

                snapshot = build_snapshot()
                policy_json = canonical_json(snapshot)
                run.policy_version = str(snapshot["policy_version"])
                uow.session.add(AgentPolicySnapshotModel(
                    id=new_id(), run_id=run.id, policy_version=run.policy_version,
                    policy_hash=hashlib.sha256(policy_json.encode()).hexdigest(),
                    payload=policy_json, signature=sign(snapshot, MASTER_KEY),
                ))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id, "chapter_id": chapter_id, "base_version_id": base_version_id}
    finally:
        await engine.dispose()


async def _read_state(settings: Settings, run_id: str):
    # 只读事务退出时 __aexit__ 会 rollback 并过期实例——必须在会话内快照为 dict
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run_snapshot = {key: getattr(run, key) for key in ("id", "status", "terminal_reason", "proposal_id", "event_cursor")}
            tasks = [
                {key: getattr(task, key) for key in ("task_key", "role", "status")}
                for task in await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id).order_by(AgentTaskModel.id))
            ]
            events = [
                {key: getattr(event, key) for key in ("sequence", "event_type", "payload")}
                for event in await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id).order_by(AgentEventModel.sequence))
            ]
            proposals = [
                {key: getattr(proposal, key) for key in ("id", "chapter_id", "base_version_id", "before_hash", "after_text", "guard_status", "status", "rationale")}
                for proposal in await uow.session.scalars(select(RevisionProposalModel))
            ]
            artifacts = [
                {key: getattr(artifact, key) for key in ("id", "task_id", "artifact_type", "payload")}
                for artifact in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run_id))
            ]
            reviews = [
                {key: getattr(review, key) for key in ("id", "status", "conflict_group")}
                for review in await uow.session.scalars(select(AgentReviewModel).where(AgentReviewModel.run_id == run_id))
            ]
            return run_snapshot, tasks, events, artifacts, reviews, proposals
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_chief_creates_merge_candidate_and_approvable_proposal(executor_settings, monkeypatch):
    provider = FakeProvider(payloads={
        "scene": {"title": "回城", "content": "雨夜，主角回城。"},
        "continuity": {"summary": "s", "findings": [{"finding": "主角位置前后矛盾", "severity": "high", "evidence_spans": [{"artifact_id": "", "start": 0, "end": 2, "quote": "雨夜"}], "verdict": "WARNING"}]},
        "chief": {"summary": "合并完成", "appendix": "合并附录：已落实连续性修订。"},
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [
        {"id": "scene", "role": "scene_writer", "token_budget": 10},
        {"id": "continuity", "role": "continuity_reviewer", "depends_on": ["scene"], "token_budget": 10},
        {"id": "merge", "role": "merge_editor", "depends_on": ["continuity"], "token_budget": 10},
        {"id": "chief", "role": "chief_editor", "depends_on": ["merge"], "token_budget": 10},
    ])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, events, artifacts, _reviews, proposals = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    assert {task["task_key"] for task in tasks if task["status"] == "SUCCEEDED"} == {"scene", "continuity", "merge", "chief"}
    # 恰好一个 proposal：before=base.content，after=base.content + "\n\n" + 模型合并附录
    assert run["proposal_id"]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["id"] == run["proposal_id"]
    assert proposal["chapter_id"] == seeded["chapter_id"]
    assert proposal["base_version_id"] == seeded["base_version_id"]
    assert proposal["before_hash"] == hashlib.sha256(BASE_CONTENT.encode("utf-8")).hexdigest()
    assert proposal["after_text"] == f"{BASE_CONTENT}\n\n合并附录：已落实连续性修订。"
    assert proposal["guard_status"] == "clear"
    assert proposal["status"] == "PROPOSED"
    # chief artifact = MergeCandidate（candidate 类型 + 四桶 + proposal 引用）
    chief_artifact = next(artifact for artifact in artifacts if json.loads(artifact["payload"]).get("proposal_id"))
    assert chief_artifact["artifact_type"] == "candidate"
    payload = json.loads(chief_artifact["payload"])
    assert payload["guard_status"] == "clear"
    assert len(payload["agreements"]) == 1  # 唯一 WARNING 评审
    assert payload["conflicts"] == []
    # 事件词表：proposal.created（带 guard_status）、两次 merge.committed（merge 任务 + chief 任务）
    created = [json.loads(event["payload"]) for event in events if event["event_type"] == "proposal.created"]
    assert len(created) == 1
    assert created[0] == {"proposal_id": proposal["id"], "guard_status": "clear"}
    assert not any(event["event_type"] == "proposal.blocked" for event in events)
    assert sum(1 for event in events if event["event_type"] == "merge.committed") == 2
    sequences = [event["sequence"] for event in events]
    assert len(set(sequences)) == len(sequences)

    # approval-bound：V2 approve 直接把提案落成新版本（guard clear → 不 422）
    from proseforge.application.revision.approve_proposal import (
        approve_persisted_proposal,
    )

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            approval = await approve_persisted_proposal(uow=uow, proposal_id=proposal["id"], user_id=seeded["user_id"], idempotency_key="approve-1")
            await uow.commit()
        assert approval.replayed is False
        assert approval.version is not None
        assert approval.version.content == proposal["after_text"]
        async with SqlAlchemyUnitOfWork(factory) as uow:
            replay = await approve_persisted_proposal(uow=uow, proposal_id=proposal["id"], user_id=seeded["user_id"], idempotency_key="approve-1")
            await uow.commit()
        assert replay.replayed is True  # 一次审批只落一个版本
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_chief_blocks_proposal_on_unresolved_conflict(executor_settings, monkeypatch):
    provider = FakeProvider(payloads={
        "scene": {"title": "回城", "content": "雨夜，主角回城。"},
        "continuity": {"summary": "s", "findings": [{"finding": "主角此时在城内", "severity": "high", "evidence_spans": [{"artifact_id": "", "start": 0, "end": 2, "quote": "雨夜"}], "verdict": "WARNING"}]},
        "adversarial": {"summary": "s", "findings": [{"finding": "主角此时在城外", "severity": "high", "evidence_spans": [{"artifact_id": "", "start": 0, "end": 2, "quote": "雨夜"}], "verdict": "WARNING"}]},
        "chief": {"summary": "合并完成", "appendix": "合并附录。"},
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [
        {"id": "scene", "role": "scene_writer", "token_budget": 10},
        {"id": "continuity", "role": "continuity_reviewer", "depends_on": ["scene"], "token_budget": 10},
        {"id": "adversarial", "role": "adversarial_reviewer", "depends_on": ["continuity"], "token_budget": 10},
        {"id": "chief", "role": "chief_editor", "depends_on": ["adversarial"], "token_budget": 10},
    ])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    _run, _tasks, events, _artifacts, reviews, proposals = await _read_state(executor_settings, seeded["run_id"])
    # 未裁定冲突仍在（两条 scene 评审互斥；第三条是 adversarial 对评审报告的 PASS）→ proposal 照常创建但 guard_status=blocked
    conflicted = [review for review in reviews if review["status"] == "CONFLICT"]
    assert len(conflicted) == 2
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["guard_status"] == "blocked"
    blocked = [json.loads(event["payload"]) for event in events if event["event_type"] == "proposal.blocked"]
    assert len(blocked) == 1
    assert blocked[0]["proposal_id"] == proposal["id"]
    assert blocked[0]["conflict_groups"] == sorted({review["conflict_group"] for review in conflicted})

    # V2 approve 路径：guard blocked → ApprovalBlocked（路由层映射为 422 REVISION_GUARD_BLOCKED）
    from proseforge.application.revision.approve_proposal import (
        ApprovalBlocked,
        approve_persisted_proposal,
    )

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            with pytest.raises(ApprovalBlocked):
                await approve_persisted_proposal(uow=uow, proposal_id=proposal["id"], user_id=seeded["user_id"], idempotency_key=None)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_chief_pipeline_rewrite_mode_produces_final_draft(executor_settings, monkeypatch):
    """M1 分流：上游同时有 SceneDraft + MergeCandidate 且 run 无章节目标
    → chief_editor 调模型按修改清单改写，产终稿 candidate（rewrite_of 指回
    scene artifact），不建 proposal。"""
    provider = FakeProvider(payloads={
        "scene": {"title": "回城", "content": "雨夜，主角回城。"},
        "chief": {"title": "回城（终稿）", "content": "雨夜，主角提着青铜钥匙回城。"},
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(
        executor_settings,
        [
            {"id": "scene", "role": "scene_writer", "token_budget": 10},
            {"id": "merge", "role": "merge_editor", "depends_on": ["scene"], "token_budget": 10},
            {"id": "chief", "role": "chief_editor", "depends_on": ["merge"], "token_budget": 10},
        ],
        with_chapter=False,
    )

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, _tasks, events, artifacts, _reviews, proposals = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    # 改写产物：终稿 candidate，rewrite_of 指回 scene artifact
    payloads = [json.loads(artifact["payload"]) for artifact in artifacts]
    rewritten = next(payload for payload in payloads if payload.get("rewrite_of"))
    assert rewritten["title"] == "回城（终稿）"
    assert rewritten["content"] == "雨夜，主角提着青铜钥匙回城。"
    scene_artifact = next(artifact for artifact in artifacts if json.loads(artifact["payload"]).get("content") == "雨夜，主角回城。")
    assert rewritten["rewrite_of"] == scene_artifact["id"]
    # 无章节目标 + 改写模式：不建 proposal
    assert run["proposal_id"] is None
    assert proposals == []
    assert not any(event["event_type"] == "proposal.created" for event in events)


@pytest.mark.asyncio
async def test_chief_with_chapter_target_keeps_proposal_flow(executor_settings, monkeypatch):
    """分流边界：SceneDraft + MergeCandidate 都在，但 run 带章节目标 →
    维持 MergeCandidate→proposal 旧链路（改写不触发）。"""
    provider = FakeProvider(payloads={
        "scene": {"title": "回城", "content": "雨夜，主角回城。"},
        "chief": {"summary": "合并完成", "appendix": "合并附录。"},
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [
        {"id": "scene", "role": "scene_writer", "token_budget": 10},
        {"id": "merge", "role": "merge_editor", "depends_on": ["scene"], "token_budget": 10},
        {"id": "chief", "role": "chief_editor", "depends_on": ["merge"], "token_budget": 10},
    ])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, _tasks, _events, artifacts, _reviews, proposals = await _read_state(executor_settings, seeded["run_id"])
    assert run["proposal_id"]
    assert len(proposals) == 1
    assert proposals[0]["after_text"] == f"{BASE_CONTENT}\n\n合并附录。"
    assert not any(json.loads(artifact["payload"]).get("rewrite_of") for artifact in artifacts)


@pytest.mark.asyncio
async def test_chief_without_chapter_produces_only_merge_candidate(executor_settings, monkeypatch):
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(
        executor_settings,
        [{"id": "chief", "role": "chief_editor", "token_budget": 10}],
        with_chapter=False,
    )

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, _tasks, events, artifacts, _reviews, proposals = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    assert run["proposal_id"] is None
    assert proposals == []
    assert provider.requests == []  # 无章节目标：不撰写附录，不调模型
    assert len(artifacts) == 1
    assert artifacts[0]["artifact_type"] == "candidate"
    assert "proposal_id" not in json.loads(artifacts[0]["payload"])
    assert not any(event["event_type"] in {"proposal.created", "proposal.blocked"} for event in events)


@pytest.mark.asyncio
async def test_chief_falls_back_to_candidate_summary_when_model_unusable(executor_settings, monkeypatch):
    provider = FakeProvider(payloads={"chief": "<<not json>>"})
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [{"id": "chief", "role": "chief_editor", "token_budget": 10}])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"  # 模型输出不可用不阻断提案链路
    run, _tasks, _events, _artifacts, _reviews, proposals = await _read_state(executor_settings, seeded["run_id"])
    assert run["proposal_id"]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["after_text"].startswith(f"{BASE_CONTENT}\n\n")
    assert "Merge of 0 reviews" in proposal["after_text"]  # 回退附录 = 候选摘要 + 桶计数
    assert proposal["guard_status"] == "clear"


def _fake_request():
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(master_key=MASTER_KEY))),
        state=SimpleNamespace(correlation_id="test-correlation"),
    )


def _body(response):
    return json.loads(response.body) if hasattr(response, "body") else response


@pytest.mark.asyncio
async def test_chief_proposal_endpoint_contract(executor_settings):
    from fastapi import HTTPException

    from proseforge.api.routes.agent_runs import chief_proposal as endpoint

    seeded = await _seed_run(
        executor_settings,
        [],
        status="COMPLETED",
        with_snapshot=True,
        reviews=[{"id": "r-warn", "status": "WARNING", "evidence": [{"quote": "雨夜"}], "payload": {"claims": [{"finding": "f"}], "resolution": None}}],
    )
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        # 新建：201 + {proposal_id, guard_status}（端点依赖注入的是未 enter 的 uow，测试同构）
        created = await endpoint(seeded["run_id"], _fake_request(), SimpleNamespace(id=seeded["user_id"]), SqlAlchemyUnitOfWork(factory))
        assert created.status_code == 201
        body = _body(created)
        assert set(body) == {"proposal_id", "guard_status"}
        assert body["guard_status"] == "clear"
        # 幂等回放：200 + 同一 proposal_id，不产生第二个提案
        replayed = await endpoint(seeded["run_id"], _fake_request(), SimpleNamespace(id=seeded["user_id"]), SqlAlchemyUnitOfWork(factory))
        assert _body(replayed) == body
        async with SqlAlchemyUnitOfWork(factory) as uow:
            count = len(list(await uow.session.scalars(select(RevisionProposalModel))))
            assert count == 1
        # 跨用户：404
        with pytest.raises(HTTPException) as not_found:
            await endpoint(seeded["run_id"], _fake_request(), SimpleNamespace(id="someone-else"), SqlAlchemyUnitOfWork(factory))
        assert not_found.value.status_code == 404
    finally:
        await engine.dispose()

    # 非 COMPLETED：409 RUN_NOT_COMPLETED
    running = await _seed_run(executor_settings, [], status="RUNNING", with_snapshot=True)
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        response = await endpoint(running["run_id"], _fake_request(), SimpleNamespace(id=running["user_id"]), SqlAlchemyUnitOfWork(factory))
        assert response.status_code == 409
        assert _body(response)["error"]["code"] == "RUN_NOT_COMPLETED"
    finally:
        await engine.dispose()

    # 快照被篡改：409 POLICY_VIOLATION + run.policy_violation 审计事件（fail-closed）
    tampered = await _seed_run(executor_settings, [], status="COMPLETED", with_snapshot=True)
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with engine.begin() as connection:
            from sqlalchemy import text

            await connection.execute(text("UPDATE agent_policy_snapshots SET signature = :sig WHERE run_id = :rid"), {"sig": "0" * 64, "rid": tampered["run_id"]})
        response = await endpoint(tampered["run_id"], _fake_request(), SimpleNamespace(id=tampered["user_id"]), SqlAlchemyUnitOfWork(factory))
        assert response.status_code == 409
        assert _body(response)["error"]["code"] == "POLICY_VIOLATION"
        async with SqlAlchemyUnitOfWork(factory) as uow:
            violations = list(await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == tampered["run_id"], AgentEventModel.event_type == "run.policy_violation")))
            assert len(violations) == 1
            assert json.loads(violations[0].payload)["decision"] == "deny"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# _rewrite_final_draft 篇幅硬要求：扩写重试 + length_shortfall 审计事件
# ---------------------------------------------------------------------------


class SequentialChiefProvider(FakeProvider):
    """chief 任务按调用顺序弹出不同 payload（模拟首轮短稿 -> 扩写重试）。"""

    def __init__(self, chief_payloads: list[object], payloads: dict[str, object] | None = None):
        super().__init__(payloads=payloads)
        self._chief_payloads = list(chief_payloads)
        self.chief_calls = 0

    async def stream(self, request):
        self.requests.append(dict(request.metadata))
        if request.metadata.get("task_key") == "chief":
            payload = self._chief_payloads[min(self.chief_calls, len(self._chief_payloads) - 1)]
            self.chief_calls += 1
        else:
            payload = self._payloads.get(request.metadata.get("task_key", ""), {"summary": "ok"})
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        yield GenerationEvent("response.started")
        yield GenerationEvent("content.delta", text=text)
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": self._input, "output_tokens": self._output, "total_tokens": self._input + self._output}})


def _rewrite_tasks() -> list[dict[str, object]]:
    return [
        {"id": "scene", "role": "scene_writer", "token_budget": 10},
        {"id": "merge", "role": "merge_editor", "depends_on": ["scene"], "token_budget": 10},
        {"id": "chief", "role": "chief_editor", "depends_on": ["merge"], "token_budget": 10},
    ]


@pytest.mark.asyncio
async def test_rewrite_retries_expansion_when_below_word_floor(executor_settings, monkeypatch):
    """首轮改写不足目标字数 → 自动扩写重试一轮，取达标的长稿，无 shortfall 事件。"""
    provider = SequentialChiefProvider(
        chief_payloads=[
            {"title": "回城（短稿）", "content": "短短的"},
            {"title": "回城（终稿）", "content": "字" * 600},
        ],
        payloads={"scene": {"title": "回城", "content": "雨夜，主角回城。"}},
    )
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(
        executor_settings, _rewrite_tasks(), with_chapter=False,
        goal="写第1章《回城》\n目标字数：不少于 500 字",
    )

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    assert provider.chief_calls == 2  # 首轮短稿触发了一次扩写重试
    _run, _tasks, events, artifacts, _reviews, _proposals = await _read_state(executor_settings, seeded["run_id"])
    rewritten = next(json.loads(artifact["payload"]) for artifact in artifacts if json.loads(artifact["payload"]).get("rewrite_of"))
    assert rewritten["content"] == "字" * 600
    assert rewritten["title"] == "回城（终稿）"
    assert not any(event["event_type"] == "chapter.length_shortfall" for event in events)


@pytest.mark.asyncio
async def test_rewrite_emits_length_shortfall_event_when_still_short(executor_settings, monkeypatch):
    """扩写重试后仍不达标：不 FAIL 整章，取较长者产出并附 chapter.length_shortfall 审计事件。"""
    provider = SequentialChiefProvider(
        chief_payloads=[
            {"title": "回城", "content": "短"},
            {"title": "回城", "content": "还是不够长"},
        ],
        payloads={"scene": {"title": "回城", "content": "雨夜，主角回城。"}},
    )
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(
        executor_settings, _rewrite_tasks(), with_chapter=False,
        goal="写第1章《回城》\n目标字数：不少于 100 字",
    )

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"  # 短章不 FAIL（批量链不断章）
    assert provider.chief_calls == 2
    _run, _tasks, events, artifacts, _reviews, _proposals = await _read_state(executor_settings, seeded["run_id"])
    rewritten = next(json.loads(artifact["payload"]) for artifact in artifacts if json.loads(artifact["payload"]).get("rewrite_of"))
    assert rewritten["content"] == "还是不够长"  # 取较长者
    shortfall = [json.loads(event["payload"]) for event in events if event["event_type"] == "chapter.length_shortfall"]
    assert shortfall == [{"actual": 5, "min_words": 100}]


# ---------------------------------------------------------------------------
# _rewrite_final_draft：叙事 RAG 场景包 + 质量门禁未过原因注入改写 prompt
# ---------------------------------------------------------------------------


class _PromptRecordingProvider(FakeProvider):
    """记录完整 user prompt 文本的假 provider（FakeProvider 只记 metadata）。"""

    async def stream(self, request):
        self.requests.append({
            "task_key": str(request.metadata.get("task_key", "")),
            "user_prompt": "\n".join(str(block.get("text", "")) for block in request.input_blocks),
        })
        payload = self._payloads.get(str(request.metadata.get("task_key", "")), {"title": "终稿", "content": "字" * 3000})
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        yield GenerationEvent("response.started")
        yield GenerationEvent("content.delta", text=text)
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": self._input, "output_tokens": self._output, "total_tokens": self._input + self._output}})


def _rewrite_context(provider: FakeProvider, **overrides) -> dict[str, object]:
    context: dict[str, object] = {
        "run": {"id": "run-1", "goal": "写第2章《云涌》\n目标字数：不少于 500 字"},
        "task": {"id": "task-1", "role": "chief_editor", "task_key": "chief"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }
    context.update(overrides)
    return context


@pytest.mark.asyncio
async def test_rewrite_prompt_carries_scene_pack_and_gate_reasons():
    # 门禁 FAIL 后的改写：prompt 必须带未过原因（总调度判定）与 RAG 未结伏笔
    from proseforge.application.agents import chief_handler

    provider = _PromptRecordingProvider()
    context = _rewrite_context(
        provider,
        scene_pack={"constraints": "未结伏笔：玉佩来历未揭晓"},
        gate_reasons=["字数不足：800 < 2500", "必含线索未命中：玉佩"],
    )
    scene = {"id": "scene-1", "title": "云涌", "content": "雨夜，主角回城。"}

    await chief_handler._rewrite_final_draft(context, scene, {"agreements": [], "conflicts": [], "unsupported": [], "accepted": []})

    prompt = str(provider.requests[0]["user_prompt"])
    assert "质量门禁未通过原因" in prompt
    assert "字数不足：800 < 2500" in prompt
    assert "必含线索未命中：玉佩" in prompt
    assert "叙事检索场景包" in prompt
    assert "玉佩来历未揭晓" in prompt
    assert "伏笔要求" in prompt


@pytest.mark.asyncio
async def test_rewrite_prompt_without_pack_or_gate_reasons_unchanged():
    # 无门禁原因 / 无场景包（门禁 PASS 或 RAG 关闭）：相关段落不出现，其余契约不变
    from proseforge.application.agents import chief_handler

    provider = _PromptRecordingProvider()
    context = _rewrite_context(provider)
    scene = {"id": "scene-1", "title": "云涌", "content": "雨夜，主角回城。"}

    await chief_handler._rewrite_final_draft(context, scene, {"agreements": []})

    prompt = str(provider.requests[0]["user_prompt"])
    assert "质量门禁未通过原因" not in prompt
    assert "叙事检索场景包" not in prompt
    assert "伏笔要求" in prompt  # 伏笔要求是改写固定契约的一部分
    assert "篇幅硬要求" in prompt



@pytest.mark.asyncio
async def test_rewrite_prompt_pinpoints_edits_and_carries_hard_fact_card():
    # 定点改写纪律 + 硬事实卡：prompt 必须带「只改清单涉及位置、其余逐字保留」
    # 指令与全书大纲提取的硬事实（七道封印/1997年）。
    from proseforge.application.agents import chief_handler

    provider = _PromptRecordingProvider()
    context = _rewrite_context(provider)
    context["run"] = {
        "id": "run-1",
        "goal": (
            "写第2章《云涌》\n目标字数：不少于 500 字\n"
            "全书大纲（仅作全局设定与伏笔参照，本章只写「写第2章」指定的内容）：\n"
            "主角解开七道封印，1997年的雨夜回城。"
        ),
    }
    scene = {"id": "scene-1", "title": "云涌", "content": "雨夜，主角回城。"}

    await chief_handler._rewrite_final_draft(context, scene, {"agreements": [], "conflicts": [], "unsupported": [], "accepted": []})

    prompt = str(provider.requests[0]["user_prompt"])
    assert "只修改清单涉及的位置" in prompt
    assert "其余正文逐字保留" in prompt
    assert "按引文定位" in prompt
    assert "本书硬事实（禁止擅改）" in prompt
    assert "七道封印" in prompt
    assert "1997年" in prompt


@pytest.mark.asyncio
async def test_rewrite_prompt_without_outline_facts_injects_no_card():
    # goal 无全书大纲、无数字事实：不注入硬事实卡，定点修改指令仍在
    from proseforge.application.agents import chief_handler

    provider = _PromptRecordingProvider()
    context = _rewrite_context(provider)
    context["run"] = {"id": "run-1", "goal": "写第2章 云涌\n目标字数：不少于 500 字"}
    scene = {"id": "scene-1", "title": "云涌", "content": "雨夜，主角回城。"}

    await chief_handler._rewrite_final_draft(context, scene, {"agreements": []})

    prompt = str(provider.requests[0]["user_prompt"])
    assert "本书硬事实（禁止擅改）" not in prompt
    assert "其余正文逐字保留" in prompt  # 定点修改指令是改写固定契约的一部分
