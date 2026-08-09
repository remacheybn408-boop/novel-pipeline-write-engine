"""运行时扩展校验（application/agents/expand_graph.validate_expansion）测试。

纯内存视图 + 一个 sqlite 集成用例（TaskRowView 来自真实落库任务行）。
证明：扩展在各类上限处停止、dedupe_key 每 run 唯一、同一 reason 每 run 只用一次、
角色 max_children 策略生效，且 graph_hash 确定可复现。
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from proseforge.application.agents.expand_graph import (
    ExpansionChild,
    ExpansionPlan,
    TaskRowView,
    graph_hash_for,
    validate_expansion,
)
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings, get_settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


def _task(key: str, *, role: str = "scene_writer", status: str = "SUCCEEDED", depends_on: tuple[str, ...] = (), token_budget: int = 1) -> TaskRowView:
    return TaskRowView(task_key=key, role=role, status=status, depends_on=depends_on, token_budget=token_budget)


def _plan(children: list[ExpansionChild] | None = None, *, reason: str = "缺少时间线核查", dedupe: str = "dedupe-1", priors: list[dict[str, object]] | None = None) -> ExpansionPlan:
    return ExpansionPlan(
        children=tuple(children if children is not None else [ExpansionChild(role="timeline_analyst", token_budget=10)]),
        expansion_reason=reason,
        dedupe_key=dedupe,
        prior_expansions=tuple(priors or []),
    )


def test_happy_path_has_no_violations():
    tasks = [_task("planner", role="chief_planner")]
    violations = validate_expansion(tasks=tasks, parent_task_key="planner", plan=_plan(), budget_limit=100, budget_used=10)
    assert violations == []


def test_parent_must_exist_and_be_terminal_succeeded():
    tasks = [_task("planner", role="chief_planner", status="RUNNING")]
    violations = validate_expansion(tasks=tasks, parent_task_key="planner", plan=_plan(), budget_limit=100, budget_used=0)
    assert "parent task is not terminal-SUCCEEDED" in violations
    missing = validate_expansion(tasks=tasks, parent_task_key="ghost", plan=_plan(), budget_limit=100, budget_used=0)
    assert "parent task not found" in missing


def test_unknown_child_role_rejected():
    plan = _plan([ExpansionChild(role="no_such_role")])
    violations = validate_expansion(tasks=[_task("planner")], parent_task_key="planner", plan=plan, budget_limit=100, budget_used=0)
    assert "unknown role: no_such_role" in violations


def test_depth_limit_stops_expansion():
    # 链 t0→t1→…→t8：t8 深度 8，再扩一层为 9，超过上限
    tasks = [_task("t0")] + [_task(f"t{index}", depends_on=(f"t{index - 1}",)) for index in range(1, 9)]
    violations = validate_expansion(tasks=tasks, parent_task_key="t8", plan=_plan(), budget_limit=100, budget_used=0)
    assert "task graph exceeds depth limit" in violations
    ok = validate_expansion(tasks=tasks[:-1], parent_task_key="t7", plan=_plan(), budget_limit=100, budget_used=0)
    assert ok == []


def test_fanout_limit_counts_existing_children():
    parent = _task("planner", role="chief_planner")
    children = [_task(f"child-{index}", depends_on=("planner",)) for index in range(8)]
    violations = validate_expansion(tasks=[parent, *children], parent_task_key="planner", plan=_plan(), budget_limit=100, budget_used=0)
    assert "task fanout exceeds limit" in violations
    assert "role child limit exceeded" in violations  # chief_planner max_children=4，两道闸同时触发
    ok = validate_expansion(tasks=[parent, *children[:3]], parent_task_key="planner", plan=_plan(), budget_limit=100, budget_used=0)
    assert ok == []


def test_remaining_budget_must_cover_children():
    plan = _plan([ExpansionChild(role="timeline_analyst", token_budget=30), ExpansionChild(role="style_editor", token_budget=30)])
    violations = validate_expansion(tasks=[_task("planner")], parent_task_key="planner", plan=plan, budget_limit=100, budget_used=50)
    assert "expansion exceeds remaining run budget" in violations
    ok = validate_expansion(tasks=[_task("planner")], parent_task_key="planner", plan=plan, budget_limit=100, budget_used=40)
    assert ok == []


def test_dedupe_key_and_reason_are_once_per_run():
    priors = [{"dedupe_key": "dedupe-1", "expansion_reason": "旧原因"}]
    violations = validate_expansion(tasks=[_task("planner")], parent_task_key="planner", plan=_plan(priors=priors), budget_limit=100, budget_used=0)
    assert "duplicate dedupe key for this run" in violations
    assert "expansion reason already used in this run" not in violations
    reused_reason = validate_expansion(tasks=[_task("planner")], parent_task_key="planner", plan=_plan(reason="旧原因", dedupe="dedupe-2", priors=priors), budget_limit=100, budget_used=0)
    assert "expansion reason already used in this run" in reused_reason
    fresh = validate_expansion(tasks=[_task("planner")], parent_task_key="planner", plan=_plan(dedupe="dedupe-2", priors=priors), budget_limit=100, budget_used=0)
    assert fresh == []


def test_role_max_children_policy_applies():
    # world_builder 的 RolePolicy.max_children = 3
    parent = _task("world", role="world_builder")
    children = [_task(f"w-{index}", depends_on=("world",)) for index in range(3)]
    violations = validate_expansion(tasks=[parent, *children], parent_task_key="world", plan=_plan(), budget_limit=100, budget_used=0)
    assert "role child limit exceeded" in violations


def test_duplicate_and_missing_dependency_keys_rejected():
    plan = _plan([
        ExpansionChild(role="scene_writer", task_key="planner"),  # 与既有任务撞 key
        ExpansionChild(role="style_editor", depends_on=("ghost",)),  # 依赖不存在的任务
    ])
    violations = validate_expansion(tasks=[_task("planner")], parent_task_key="planner", plan=plan, budget_limit=100, budget_used=0)
    assert "duplicate task key: planner" in violations
    assert "child style_editor-2 dependency must reference an existing task: ghost" not in violations  # 默认 key 形为 parent-expand-N
    assert any(violation.startswith("child planner-expand-2 dependency must reference an existing task") for violation in violations)


def test_node_limit_stops_expansion():
    tasks = [_task(f"t{index}") for index in range(64)]
    plan = _plan([ExpansionChild(role="scene_writer")])
    violations = validate_expansion(tasks=tasks, parent_task_key="t0", plan=plan, budget_limit=1000, budget_used=0)
    assert "task graph exceeds node limit" in violations


def test_graph_hash_is_deterministic():
    left = graph_hash_for([_task("b"), _task("a", depends_on=("b",))])
    right = graph_hash_for([_task("a", depends_on=("b",)), _task("b")])
    assert left == right


@pytest.fixture()
def runtime_settings(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'expand.db').as_posix()}"
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


@pytest.mark.asyncio
async def test_task_views_from_persisted_rows_validate(runtime_settings):
    """TaskRowView 由真实落库的 AgentTaskModel 行构造时校验语义一致（端点所用路径）。"""
    engine, factory = create_engine_and_sessionmaker(runtime_settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            # FK parents: agent_tasks.run_id -> agent_runs.id -> projects.id.
            # Flush each parent before its children (relationship-less tables
            # flush in arbitrary order at commit).
            uow.session.add(ProjectModel(id="project-1", owner_id="u1", slug="project-1", title="Expand Test Project"))
            await uow.session.flush()
            now = datetime.now(UTC)
            uow.session.add(AgentRunModel(
                id="run-1", user_id="u1", project_id="project-1", goal_hash="g" * 64,
                graph_revision=1, status="RUNNING", budget_limit=100,
                created_at=now, updated_at=now,
            ))
            await uow.session.flush()
            uow.session.add(AgentTaskModel(id=new_id(), run_id="run-1", task_key="planner", role="chief_planner", status="SUCCEEDED", token_budget=5, depends_on="[]"))
            uow.session.add(AgentTaskModel(id=new_id(), run_id="run-1", task_key="scene", role="scene_writer", status="RUNNING", token_budget=5, depends_on=json.dumps(["planner"])))
            await uow.commit()
        async with SqlAlchemyUnitOfWork(factory) as uow:
            from sqlalchemy import select

            rows = await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == "run-1").order_by(AgentTaskModel.id))
            views = [TaskRowView(task_key=row.task_key, role=row.role, status=row.status, depends_on=tuple(json.loads(row.depends_on)), token_budget=row.token_budget) for row in rows]
        assert validate_expansion(tasks=views, parent_task_key="planner", plan=_plan(), budget_limit=100, budget_used=0) == []
        blocked = validate_expansion(tasks=views, parent_task_key="scene", plan=_plan(), budget_limit=100, budget_used=0)
        assert blocked == ["parent task is not terminal-SUCCEEDED"]
    finally:
        await engine.dispose()
