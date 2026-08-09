"""持久化作用域记忆服务（蓝图 V3-005 memory 部分 / V3-008）。

与纯内存版 ``scoped_memory.ScopedMemory``（保留给既有测试）不同，本模块把
记忆候选落库到 ``agent_memories`` 表（迁移 0020）：

- 候选一律以 ``PENDING`` 写入，携带来源 Artifact、置信度与 revision 计数器；
  表无 confidence/revision 列，二者编码进 ``value`` JSON 信封
  ``{"value": ..., "confidence": ..., "revision": ...}``（不加迁移）。
- 只有用户审批（accept 端点）能把记忆翻成 ``ACCEPTED``/``REJECTED``；
  Agent 角色无 ``activate_memory_fact`` 能力（domain/agents/policy.py 恒拒）。
- 作用域为 (project_id, run_id)；表 run_id 列非空，项目级记忆用空串
  ``""`` 作为 run_id 哨兵值（见 ``PROJECT_WIDE_RUN``）。
- ``load_memory_slice`` 给默认 role handler 提供已批准记忆切片，带显式
  条数与长度上限（token 预算见 prompts.build_task_prompt）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.agents.policy import authorize
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.agents import AgentMemoryModel

MEMORY_STATUSES: tuple[str, ...] = ("PENDING", "ACCEPTED", "REJECTED")
PROJECT_WIDE_RUN = ""  # run_id 哨兵：项目级（跨 run 可见）记忆
MEMORY_SLICE_LIMIT = 8  # 单次注入提示词的记忆条数上限
MEMORY_VALUE_MAX_CHARS = 200  # 单条记忆值注入上限（字符）
ACTIVATION_CAPABILITY = "activate_memory_fact"

# 并行任务突发共享的微缓存：executor 每批次认领时快照一次 run["memory_slice"]
# 放进 TaskContext（workflows/agent_executor.py），默认 handler 直接读快照；
# 无快照的直连路径（测试、独立调用）才每任务调一次 load_memory_slice；
# 无缓存时 16 路并发各自开新连接（sqlite PRAGMA 建连开销）会把并行峰值打散。
# 键 = (database_url, project_id, run_id)；TTL 内命中零连接开销。decide 端点
# 提交后调 invalidate_memory_slice_cache() 立即可见；跨进程最迟 TTL 秒可见。
_SLICE_CACHE_TTL_SECONDS = 2.0
_SLICE_CACHE_MAX_KEYS = 64
_slice_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, object]]]] = {}
# 全局加载锁：同一突发内只有第一个任务真正查库，其余等锁后命中 TTL 缓存——
# 否则 16 路并发会在首个查询完成前全部 miss，各自开新连接（sqlite PRAGMA
# 建连开销）打散并行峰值。锁只在加载路径上持有，TTL 命中不进锁。
# 注意：asyncio.Lock 首次等待时绑定当前事件循环，而 worker 每个任务可能跑在
# 新的 asyncio.run 循环里；模块级单例锁跨循环复用会抛
# "Lock is bound to a different event loop"。因此按 (loop, lock) 惰性创建，
# 每个事件循环各持一把——互斥突发本就跑在同一循环内，语义不变。
_slice_load_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def _slice_lock() -> asyncio.Lock:
    global _slice_load_lock
    loop = asyncio.get_running_loop()
    if _slice_load_lock is None or _slice_load_lock[0] is not loop:
        _slice_load_lock = (loop, asyncio.Lock())
    return _slice_load_lock[1]


def invalidate_memory_slice_cache() -> None:
    _slice_cache.clear()


def assert_agent_activation_denied(role: str) -> None:
    """复用 domain 策略：任何 Agent 角色激活记忆事实都必须被拒（测试与审计用）。

    用户审批不经过本函数——accept 端点是用户会话动作，不走角色授权。
    """
    authorize(role, ACTIVATION_CAPABILITY)


def encode_value(value: object, *, confidence: float, revision: int) -> str:
    return json.dumps({"value": value, "confidence": confidence, "revision": revision}, ensure_ascii=False, sort_keys=True)


def decode_value(row: AgentMemoryModel) -> dict[str, object]:
    """读 value 信封；历史非 JSON 纯文本按 {"value": 原文} 兜底。"""
    try:
        parsed = json.loads(row.value)
    except ValueError:
        return {"value": row.value, "confidence": None, "revision": None}
    if isinstance(parsed, dict) and "value" in parsed:
        return {"value": parsed["value"], "confidence": parsed.get("confidence"), "revision": parsed.get("revision")}
    return {"value": parsed, "confidence": None, "revision": None}


async def propose_memory(
    session: AsyncSession,
    *,
    project_id: str,
    run_id: str,
    memory_key: str,
    value: object,
    source_artifact_id: str,
    confidence: float = 1.0,
    status: str = "PENDING",
) -> AgentMemoryModel:
    """写入记忆候选；revision = 同 (project_id, memory_key) 已有条数 + 1。

    status 默认 PENDING（走用户审批）。管线沉淀的自动事实传 "ACCEPTED"
    直接激活——批量写作链不能被逐条人工审批卡死；来源以 run_id=
    PROJECT_WIDE_RUN + source_artifact_id=章节 id 记账，可审计可回查。
    """
    if status not in MEMORY_STATUSES:
        raise ValueError(f"unknown memory status: {status}")
    existing = int(
        await session.scalar(
            select(func.count(AgentMemoryModel.id)).where(
                AgentMemoryModel.project_id == project_id, AgentMemoryModel.memory_key == memory_key
            )
        )
        or 0
    )
    row = AgentMemoryModel(
        id=new_id(),
        project_id=project_id,
        run_id=run_id,
        memory_key=memory_key,
        value=encode_value(value, confidence=confidence, revision=existing + 1),
        source_artifact_id=source_artifact_id,
        status=status,
    )
    session.add(row)
    return row


async def list_memories(
    session: AsyncSession,
    *,
    project_id: str,
    run_id: str | None = None,
    status: str | None = None,
) -> list[AgentMemoryModel]:
    query = select(AgentMemoryModel).where(AgentMemoryModel.project_id == project_id)
    if run_id is not None:
        query = query.where(AgentMemoryModel.run_id == run_id)
    if status is not None:
        query = query.where(AgentMemoryModel.status == status)
    return list(await session.scalars(query.order_by(AgentMemoryModel.id)))


def decide_memory(row: AgentMemoryModel, decision: str) -> None:
    """用户审批翻转状态；只允许 accept/reject。"""
    row.status = {"accept": "ACCEPTED", "reject": "REJECTED"}[decision]


def memory_view(row: AgentMemoryModel) -> dict[str, object]:
    envelope = decode_value(row)
    return {
        "id": row.id,
        "memory_key": row.memory_key,
        "value": envelope["value"],
        "confidence": envelope["confidence"],
        "revision": envelope["revision"],
        "status": row.status,
        "source_artifact_id": row.source_artifact_id,
    }


def _slice_cache_key(uow_factory: Callable[[], object], project_id: str, run_id: str) -> tuple[str, str, str] | None:
    """从 uow_factory 未启动的 uow 上取 engine URL 作缓存键（不占用连接）。"""
    try:
        uow = uow_factory()
        bind = getattr(getattr(uow, "session_factory", None), "kw", {}).get("bind")
    except Exception:
        return None
    url = str(getattr(bind, "url", "") or "")
    return (url, project_id, run_id) if url else None


async def load_memory_slice(
    uow_factory: Callable[[], object] | None,
    run: dict[str, object],
    *,
    limit: int = MEMORY_SLICE_LIMIT,
) -> list[dict[str, object]]:
    """默认 role handler 的记忆切片：项目内本 run（含项目级）的 ACCEPTED 记忆。

    ``uow_factory`` 缺失（纯内存测试 context）或无 project_id 时返回空切片。
    每条形如 {"fact_key": ..., "value": ...}，值按 ``MEMORY_VALUE_MAX_CHARS`` 截断。
    同一 (database_url, project_id, run_id) 的并发突发共享 TTL 微缓存（见模块头注释）。
    """
    if uow_factory is None:
        return []
    project_id = str(run.get("project_id", "") or "")
    run_id = str(run.get("id", "") or "")
    if not project_id:
        return []
    key = _slice_cache_key(uow_factory, project_id, run_id)
    now = time.monotonic()
    if key is not None:
        cached = _slice_cache.get(key)
        if cached is not None and now - cached[0] < _SLICE_CACHE_TTL_SECONDS:
            return [dict(item) for item in cached[1]]
    async with _slice_lock():
        if key is not None:  # 等锁期间同突发任务可能已完成加载：双重检查
            cached = _slice_cache.get(key)
            now = time.monotonic()
            if cached is not None and now - cached[0] < _SLICE_CACHE_TTL_SECONDS:
                return [dict(item) for item in cached[1]]
        async with uow_factory() as uow:  # type: ignore[operator]
            rows = await list_memories(uow.session, project_id=project_id, status="ACCEPTED")
            scoped = [row for row in rows if row.run_id in {run_id, PROJECT_WIDE_RUN}]
            # 同 key 只留最新 revision（管线每章重写的状态事实不叠旧值）；
            # 取最近写入的 limit 条——先查再想查的是「当下最相关」，
            # 不是最早入库的历史事实。
            latest: dict[str, AgentMemoryModel] = {}
            for row in scoped:
                revision = decode_value(row).get("revision")
                current = latest.get(row.memory_key)
                if current is None or (revision or 0) >= (decode_value(current).get("revision") or 0):
                    latest[row.memory_key] = row
            recent = sorted(latest.values(), key=lambda row: row.id)[-limit:]
            result = [
                {"fact_key": row.memory_key, "value": str(decode_value(row)["value"])[:MEMORY_VALUE_MAX_CHARS]}
                for row in recent
            ]
        if key is not None:
            if len(_slice_cache) >= _SLICE_CACHE_MAX_KEYS:
                _slice_cache.pop(min(_slice_cache, key=lambda cache_key: _slice_cache[cache_key][0]))
            _slice_cache[key] = (now, result)
        return result
