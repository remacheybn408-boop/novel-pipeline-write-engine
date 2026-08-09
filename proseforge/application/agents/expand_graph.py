"""任务图扩展（蓝图 V3-008 dynamic expansion）。

两部分：
- ``expand_graph``：纯内存 TaskGraph 扩展（既有契约，保留）。
- 运行时扩展校验：``ExpansionChild`` / ``validate_expansion``，在
  validate_graph 的静态上限（64 节点 / 8 深度 / 8 扇出）之上叠加服务端
  运行时规则——父任务须 terminal-SUCCEEDED、角色目录、剩余预算、
  dedupe_key 每 run 唯一、同一 expansion_reason 每 run 只用一次、
  角色 max_children 策略（domain/agents/policy.check_children）。
  模型输出不能直接建任务：只有用户会话的 expand 端点能落新任务行。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from proseforge.application.agents.validate_graph import validate_graph
from proseforge.domain.agents.policy import PolicyDenied, check_children
from proseforge.domain.agents.roles import AgentRole
from proseforge.domain.agents.task_graph import AgentTaskSpec, TaskGraph

MAX_GRAPH_NODES = 64
MAX_GRAPH_DEPTH = 8
MAX_TASK_FANOUT = 8
MAX_TASK_FANIN = 8


def expand_graph(graph: TaskGraph, parent_task_id: str, task: AgentTaskSpec, reason: str) -> TaskGraph:
    if not reason.strip(): raise ValueError("expansion reason is required")
    if parent_task_id not in {item.id for item in graph.tasks}: raise ValueError("parent task not found")
    if task.id in {item.id for item in graph.tasks}: raise ValueError("duplicate expansion")
    expanded = TaskGraph(graph.revision + 1, graph.tasks + (task,)); validate_graph(expanded); return expanded


@dataclass(frozen=True)
class TaskRowView:
    """持久化任务行（AgentTaskModel）的校验视图。"""

    task_key: str
    role: str
    status: str
    depends_on: tuple[str, ...] = ()
    token_budget: int = 1


@dataclass(frozen=True)
class ExpansionChild:
    role: str
    task_key: str | None = None
    depends_on: tuple[str, ...] | None = None  # None → 默认 [父任务]
    input_artifact_types: tuple[str, ...] = ()
    output_artifact_type: str = "report"
    token_budget: int = 1
    permission_profile: str = "default"


@dataclass(frozen=True)
class ExpansionPlan:
    children: tuple[ExpansionChild, ...]
    expansion_reason: str
    dedupe_key: str
    prior_expansions: tuple[dict[str, object], ...] = field(default_factory=tuple)  # 既有 graph revision payload


def _depths(tasks: list[TaskRowView]) -> dict[str, int]:
    deps = {task.task_key: task.depends_on for task in tasks}
    memo: dict[str, int] = {}

    def depth(key: str, seen: frozenset[str]) -> int:
        if key in memo:
            return memo[key]
        if key in seen or key not in deps:
            return 0
        value = max((depth(parent, seen | {key}) + 1 for parent in deps[key]), default=0)
        memo[key] = value
        return value

    return {task.task_key: depth(task.task_key, frozenset()) for task in tasks}


def graph_hash_for(tasks: list[TaskRowView]) -> str:
    canonical = json.dumps(
        sorted(({"task_key": t.task_key, "role": t.role, "depends_on": sorted(t.depends_on)} for t in tasks), key=lambda item: item["task_key"]),
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_expansion(*, tasks: list[TaskRowView], parent_task_key: str, plan: ExpansionPlan, budget_limit: int, budget_used: int) -> list[str]:
    """运行时扩展校验；返回违规列表（空 = 通过），顺序确定。"""
    violations: list[str] = []
    by_key = {task.task_key: task for task in tasks}
    parent = by_key.get(parent_task_key)
    if parent is None:
        violations.append("parent task not found")
    elif parent.status != "SUCCEEDED":
        violations.append("parent task is not terminal-SUCCEEDED")
    if not plan.expansion_reason.strip():
        violations.append("expansion reason is required")
    if not plan.dedupe_key.strip():
        violations.append("dedupe key is required")
    if any(str(prior.get("dedupe_key", "")) == plan.dedupe_key for prior in plan.prior_expansions):
        violations.append("duplicate dedupe key for this run")
    if any(str(prior.get("expansion_reason", "")) == plan.expansion_reason for prior in plan.prior_expansions):
        violations.append("expansion reason already used in this run")
    if not plan.children:
        violations.append("at least one child task is required")
    if len(tasks) + len(plan.children) > MAX_GRAPH_NODES:
        violations.append("task graph exceeds node limit")
    child_keys: set[str] = set()
    for index, child in enumerate(plan.children):
        key = child.task_key or f"{parent_task_key}-expand-{index + 1}"
        if child.role not in {role.value for role in AgentRole}:
            violations.append(f"unknown role: {child.role}")
        if child.token_budget <= 0:
            violations.append(f"child {key} token budget must be positive")
        if key in by_key or key in child_keys:
            violations.append(f"duplicate task key: {key}")
        child_keys.add(key)
        deps = child.depends_on if child.depends_on is not None else (parent_task_key,)
        if len(deps) > MAX_TASK_FANIN:
            violations.append(f"child {key} fan-in exceeds limit")
        for dependency in deps:
            if dependency not in by_key:
                violations.append(f"child {key} dependency must reference an existing task: {dependency}")
                break
    if parent is not None:
        existing_children = sum(1 for task in tasks if parent_task_key in task.depends_on)
        if existing_children + len(plan.children) > MAX_TASK_FANOUT:
            violations.append("task fanout exceeds limit")
        if _depths(tasks).get(parent_task_key, 0) + 1 > MAX_GRAPH_DEPTH:
            violations.append("task graph exceeds depth limit")
        try:
            check_children(parent.role, existing_children + len(plan.children))
        except PolicyDenied:
            violations.append("role child limit exceeded")
        except (KeyError, ValueError):
            violations.append(f"unknown role: {parent.role}")
    requested = sum(child.token_budget for child in plan.children)
    if requested > budget_limit - budget_used:
        violations.append("expansion exceeds remaining run budget")
    return violations
