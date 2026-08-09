"""promise_keeper（奥莉维亚·动态承诺）专家 handler：契约 / 核对 / 登记三节点。

write 图三个 promise_* task_key 由 ``promise_keeper_handler`` 按 task_key 分派：

- promise_contract（character 后、scene_a/b/c 前）：双路取证——承诺台账
  （project_id + kind=promise + status∈RETRIEVABLE 直查，外加 character_state）
  与叙事 RAG（goal 摘要组查询串 → keyword_leg + vector_leg → rrf_fuse top N）——
  调模型产出「本章承诺契约卡」JSON artifact（due/plant/watch）。契约卡由
  scene_writer（role_handlers）与 chief_editor 改写（chief_handler）注入；
  模型/检索失败或 artifact 缺失时降级为空契约，注入处不注入，链路不退化。
- promise_verify（recheck 后）：终稿 + 契约卡 due 清单 → 模型逐条判
  fulfilled + 证据引文 → best-effort 落库：fulfilled 追加
  value_json.fulfillments[{chapter,quote}]，累计 ≥ required_fulfillments
  （默认 1）按 PROMISE_TRANSITIONS 打勾 open→developing→resolved（version+1）；
  未兑现记 chapter.promise_missed 审计事件（advisory，不进门禁 reason）。
  取证方式两路（PROSEFORGE_PROMISE_RAG_VERIFY 开关，默认 off 走旧路）：
  旧路全章通读（终稿 elide 后整体进 prompt）；新路「承诺→段落」RAG 定向
  取证——每条 due 承诺定向检索历史章节证据片段（本章草稿不在索引里，
  索引在写回时才入队）+ 本章直查分段扫描（确定性打分取相关段落），
  任一条目两路皆空或索引异常 → 显式回落全章通读旧路 + 落
  chapter.promise_rag_verify_fallback 审计事件，索引异常绝不影响出结果。
- promise_register（图尾）：终稿 + goal 伏笔/钩子行 + 现有 open/developing
  清单 → 模型提取新承诺并语义判重：新承诺建行（status=open, source="auto",
  confidence=0.6, value_json.category∈伏笔/钩子/承诺/受伤/奖励），判重命中并入
  既有行（version+1，note 合并，不删行）。与确定性 promise_tracker 共存不冲突。

所有落库都是 best-effort：DB/模型异常只降级 artifact 内容，绝不让
promise_* 任务失败拖垮写作管线（executor 对非 recheck 任务的失败语义是
run FAILED，本模块自行吞掉可恢复异常）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from proseforge.application.agents.artifact_texts import elide_middle
from proseforge.application.agents.prompts import (
    JSON_OUTPUT_INSTRUCTION,
    goal_hint_for,
    prompt_for_role,
    render_memory_lines,
)
from proseforge.application.agents.review_handlers import stream_model_json
from proseforge.application.agents.review_target import parse_chapter_no
from proseforge.application.agents.role_handlers import (
    RoleResult,
    TaskContext,
    default_role_handler,
    register_role,
)
from proseforge.application.story_bible.promise_tracker import parse_goal_hooks
from proseforge.domain.story_bible.entities import (
    PROMISE_TRANSITIONS,
    RETRIEVABLE_STATUSES,
    StoryFact,
)
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentRunModel,
)
from proseforge.infrastructure.database.models.retrieval import RetrievalChunkModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel

logger = logging.getLogger(__name__)

# 契约卡注入上限（超出截断），与 role_handlers.SCENE_BRIDGE_MAX_CHARS 同级。
PROMISE_CONTRACT_MAX_CHARS = 1200
# RAG 融合取证条数与终稿注入上限（chars；无 input_budget 的纯测试 context 兜底）。
RAG_FUSED_TOP = 6
FINAL_DRAFT_FALLBACK_MAX_CHARS = 20000
# verify 本章直查分段扫描上限：每条 due 承诺取 top 段落数与总字符预算。
VERIFY_DIRECT_SCAN_MAX_PARAGRAPHS = 6
VERIFY_DIRECT_SCAN_MAX_CHARS = 1500
PROMISE_CATEGORIES = ("伏笔", "钩子", "承诺", "受伤", "奖励")

_CONTRACT_INSTRUCTION = (
    "产出本章承诺契约卡，只依据上面的台账、钩子行与证据片段判断，逐条带依据：\n"
    '- due：本章应兑现的承诺/伏笔（goal 钩子行的回收/照应项必收；台账里语义上到期的也收），'
    '每条形如 {"key": "台账 key 或钩子原文", "source_chapter": 埋设章号(整数,未知填 null), '
    '"evidence": "证据引文", "required_fulfillments": 需要兑现总次数(默认 1), '
    '"remaining": 剩余次数, "reason": "为何本章应兑现"}\n'
    '- plant：本章应埋入的新钩子，每条形如 {"hook": "...", "note": "埋入方式建议"}\n'
    '- watch：受伤/奖励/人物状态提醒，每条形如 {"topic": "...", "note": "..."}\n'
    '输出形如 {"summary": "...", "due": [...], "plant": [...], "watch": [...]}，无内容的栏给空数组。'
)

_VERIFY_INSTRUCTION = (
    "逐条核对 due 清单在终稿正文中是否兑现：只在正文能找到明确对应情节时判 fulfilled=true，"
    "并给出正文引文 quote（出自终稿原文）；找不到或无明确对应判 fulfilled=false，quote 给空串。\n"
    '输出形如 {"verdicts": [{"key": "due 条目 key 原文", "fulfilled": true, "quote": "..."}]}，'
    "verdicts 必须覆盖 due 清单的每一条，key 原样照抄不得改写。"
)

_REGISTER_INSTRUCTION = (
    "从终稿正文提取本章新埋入的承诺（伏笔/钩子/承诺/受伤/奖励），goal 钩子行的埋入项必收，"
    "key 尽量使用钩子原文：\n"
    '- 每条形如 {"key": "...", "category": "伏笔|钩子|承诺|受伤|奖励", "note": "一句话说明", '
    '"duplicate_of": 与既有台账语义重复时填既有 key，否则填 null}\n'
    "- 只收本章新埋入的；已在既有清单中出现的（含语义重复）不要当新承诺，标 duplicate_of 即可。\n"
    '输出形如 {"promises": [...]}，没有新承诺给空数组。'
)


# ---------------------------------------------------------------------------
# 共享取证与渲染
# ---------------------------------------------------------------------------


async def _load_ledger(session, project_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """承诺台账 + 人物状态直查快照（kind=promise/character_state，RETRIEVABLE 状态）。"""
    rows = (await session.scalars(
        select(StoryBibleEntryModel).where(
            StoryBibleEntryModel.project_id == project_id,
            StoryBibleEntryModel.kind.in_(("promise", "character_state")),
            StoryBibleEntryModel.status.in_(RETRIEVABLE_STATUSES),
        ).order_by(StoryBibleEntryModel.kind, StoryBibleEntryModel.key)
    )).all()
    promises: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    for row in rows:
        try:
            value = json.loads(row.value_json or "{}")
        except ValueError:
            value = {}
        snapshot = {"id": row.id, "key": row.key, "status": row.status, "value": value}
        (promises if row.kind == "promise" else states).append(snapshot)
    return promises, states


async def _embed_query(uow_factory: Any, owner_id: str, query: str) -> tuple[list[float], str] | None:
    """解析用户嵌入引擎并向量化查询串；仅 api 引擎（本地引擎 avoid 在 run 内触发模型下载）。

    任何失败返回 None（vector leg 缺位，keyword leg 照常）。
    """
    from proseforge.application.retrieval.indexing import _resolve_embedding_engine
    from proseforge.settings import get_settings

    async with uow_factory() as uow:  # type: ignore[operator]
        engine = await _resolve_embedding_engine(uow, owner_id, get_settings().master_key.get_secret_value())
    if engine is None or engine.kind != "api" or engine.embedder is None:
        return None
    result = await engine.embedder.embed([query])
    vector = result.vectors[0] if result.vectors else None
    if not vector:
        return None
    return list(vector), engine.identity


async def _rag_evidence(
    session,
    uow_factory: Any,
    *,
    project_id: str,
    owner_id: str,
    query: str,
    entities: list[str],
) -> list[str]:
    """双路检索证据片段：keyword_leg 恒可用；vector_leg 仅在 PG + api 嵌入引擎时参与。"""
    from proseforge.application.retrieval.search import (
        keyword_leg,
        rrf_fuse,
        vector_leg,
    )

    legs: list[list[Any]] = []
    try:
        legs.append(await keyword_leg(session, project_id=project_id, query=query, entities=entities))
    except Exception:
        logger.warning("promise contract keyword leg failed project=%s", project_id, exc_info=True)
    vector_hits: list[Any] = []
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        try:
            embedded = await _embed_query(uow_factory, owner_id, query)
            if embedded is not None:
                vector_hits = await vector_leg(session, project_id=project_id, query_vector=embedded[0], identity=embedded[1])
        except Exception:
            logger.warning("promise contract vector leg failed project=%s", project_id, exc_info=True)
    legs.append(vector_hits)
    fused = rrf_fuse(legs, top=RAG_FUSED_TOP)
    if not fused:
        return []
    rows = await session.scalars(select(RetrievalChunkModel).where(RetrievalChunkModel.id.in_([chunk_id for chunk_id, _score in fused])))
    content_by_id = {row.id: row.content for row in rows}
    return [content_by_id[chunk_id][:400] for chunk_id, _score in fused if chunk_id in content_by_id]


def render_promise_contract(payload: object, *, max_chars: int = PROMISE_CONTRACT_MAX_CHARS) -> str:
    """契约卡 payload → 注入用紧凑文本；空契约/非法 payload 返回 ""。

    注入处（scene_writer / chief_editor 改写）在返回空串时不注入契约块，
    这就是「artifact 缺失/降级不注入」的降级点。
    """
    if not isinstance(payload, dict) or payload.get("degraded"):
        return ""
    lines: list[str] = []
    due = [item for item in payload.get("due") or [] if isinstance(item, dict) and str(item.get("key") or "").strip()]
    if due:
        lines.append("本章应兑现（due，必须在正文中正面落实为具体情节）：")
        for item in due:
            parts = [f"「{str(item['key']).strip()}」"]
            source_chapter = item.get("source_chapter")
            if isinstance(source_chapter, int) and source_chapter > 0:
                parts.append(f"埋设于第{source_chapter}章")
            remaining = item.get("remaining", item.get("required_fulfillments"))
            if isinstance(remaining, int) and remaining > 1:
                parts.append(f"剩余兑现 {remaining} 次")
            if str(item.get("reason") or "").strip():
                parts.append(str(item["reason"]).strip())
            if str(item.get("evidence") or "").strip():
                parts.append(f"依据：{str(item['evidence']).strip()}")
            lines.append("- " + "；".join(parts))
    plant = [item for item in payload.get("plant") or [] if isinstance(item, dict) and str(item.get("hook") or "").strip()]
    if plant:
        lines.append("本章应埋入（plant，落实为具体细节，忌一笔带过）：")
        for item in plant:
            note = str(item.get("note") or "").strip()
            lines.append(f"- {str(item['hook']).strip()}" + (f"（{note}）" if note else ""))
    watch = [item for item in payload.get("watch") or [] if isinstance(item, dict) and str(item.get("topic") or "").strip()]
    if watch:
        lines.append("状态提醒（watch，与台账保持一致）：")
        for item in watch:
            note = str(item.get("note") or "").strip()
            lines.append(f"- {str(item['topic']).strip()}" + (f"：{note}" if note else ""))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


async def load_promise_contract_card(context: TaskContext) -> str:
    """读 promise_contract Artifact 渲染注入文本；缺失/损坏/降级返回 ""（不注入）。

    与 role_handlers._load_scene_bridge 同款模式：uow_factory 缺失（纯内存
    测试 context）、上游无契约 Artifact、payload 非 JSON 均静默返回空串。
    """
    uow_factory = context.get("uow_factory")
    if uow_factory is None:
        return ""
    contract_ids = [
        str(item.get("id", ""))
        for item in context.get("artifacts", [])
        if isinstance(item, dict) and str(item.get("task_key", "")) == "promise_contract" and item.get("id")
    ]
    if not contract_ids:
        return ""
    async with uow_factory() as uow:  # type: ignore[operator]
        row = await uow.session.get(AgentArtifactModel, contract_ids[-1])
        raw_payload = row.payload if row is not None else None  # 会话内快照，退出后 ORM 实例过期
    if raw_payload is None:
        return ""
    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError):
        return ""
    return render_promise_contract(payload)


async def _load_final_draft(session, artifact_ids: list[str]) -> dict[str, Any] | None:
    """终稿正文快照：rewrite（rewrite_of）> select 择优（selected_from）> 首个带正文草稿。

    与 chief_handler 的终稿血缘同款判定；门禁通过（改写链 SKIPPED）时终稿
    即 select 优胜稿。无正文 artifact 返回 None。
    """
    if not artifact_ids:
        return None
    final: dict[str, Any] | None = None
    for row in await session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.id.in_(artifact_ids)).order_by(AgentArtifactModel.id)):
        try:
            payload = json.loads(row.payload)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        content = payload.get("content")
        if isinstance(content, str) and content.strip() and (payload.get("rewrite_of") or payload.get("selected_from") or final is None):
            final = {"title": str(payload.get("title") or ""), "content": content}
    return final


async def _load_contract_payload(session, artifact_ids: list[str]) -> dict[str, Any] | None:
    """最新 promise_contract Artifact 的 payload；缺失/损坏/降级返回 None。"""
    if not artifact_ids:
        return None
    rows = (await session.scalars(
        select(AgentArtifactModel).where(AgentArtifactModel.id.in_(artifact_ids)).order_by(AgentArtifactModel.id)
    )).all()
    for row in reversed(rows):
        try:
            payload = json.loads(row.payload)
        except ValueError:
            continue
        if isinstance(payload, dict) and not payload.get("degraded"):
            return payload
    return None


def _context_snapshot(context: TaskContext) -> tuple[dict[str, Any], dict[str, Any], str, list[str]]:
    """公共快照：run/task dict、project_id 与上游 artifact id 清单。"""
    run = context["run"]
    task = context["task"]
    assert isinstance(run, dict) and isinstance(task, dict)
    project_id = str(run.get("project_id") or "")
    artifact_ids = [str(item.get("id", "")) for item in context.get("artifacts", []) if isinstance(item, dict) and item.get("id")]
    return run, task, project_id, artifact_ids


def _due_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """契约卡 due 清单归一：只留带 key 的 dict 条目。"""
    return [item for item in payload.get("due") or [] if isinstance(item, dict) and str(item.get("key") or "").strip()]


# ---------------------------------------------------------------------------
# promise_contract：台账 + RAG 双路取证 → 契约卡 artifact
# ---------------------------------------------------------------------------


def _normalize_contract(output: dict[str, Any]) -> dict[str, Any]:
    """模型输出归一为契约卡 payload（due/plant/watch 只留合法条目）。"""
    def _dict_list(key: str) -> list[dict[str, Any]]:
        return [dict(item) for item in output.get(key) or [] if isinstance(item, dict)]

    return {
        "summary": str(output.get("summary") or ""),
        "due": _dict_list("due"),
        "plant": _dict_list("plant"),
        "watch": _dict_list("watch"),
    }


async def _run_contract(context: TaskContext) -> RoleResult:
    run, task, project_id, _artifact_ids = _context_snapshot(context)
    goal = str(run.get("goal") or "")
    uow_factory = context.get("uow_factory")
    ledger_promises: list[dict[str, Any]] = []
    character_states: list[dict[str, Any]] = []
    evidence: list[str] = []
    if uow_factory is not None and project_id:
        try:
            async with uow_factory() as uow:  # type: ignore[operator]
                owner_id = str(await uow.session.scalar(select(AgentRunModel.user_id).where(AgentRunModel.id == str(run["id"]))) or "")
                ledger_promises, character_states = await _load_ledger(uow.session, project_id)
                # 本章大纲摘要/标题组查询串：goal 头部压白截取（batch 目标行首即章题与大纲）。
                query = " ".join(goal.split())[:300]
                entities = [str(state["key"]) for state in character_states][:10]
                evidence = await _rag_evidence(uow.session, uow_factory, project_id=project_id, owner_id=owner_id, query=query, entities=entities)
        except Exception:
            logger.warning("promise contract evidence build failed run=%s", run.get("id"), exc_info=True)
            ledger_promises, character_states, evidence = [], [], []

    lines = [
        f"任务：{task.get('task_key', '')}（角色 promise_keeper）",
        f"写作目标摘要：{goal_hint_for(run)}",
    ]
    # 记忆优先（先查再想）：契约构建参照已批准记忆（人物状态/时间线等）。
    lines.extend(render_memory_lines([item for item in run.get("memory_slice") or [] if isinstance(item, dict)]))
    hooks = parse_goal_hooks(goal)
    if hooks:
        lines.append("本章 goal 伏笔/钩子行（确定性解析，回收/照应项必须进 due，埋入项进 plant）：")
        lines.extend(f"- {'埋入' if action == 'plant' else '回收'}：{clue}" for action, clue in hooks)
    if ledger_promises:
        lines.append("承诺台账（open/developing，附已兑现次数）：")
        for promise in ledger_promises:
            value = promise["value"]
            fulfilled_count = len(value.get("fulfillments") or [])
            note = str(value.get("note") or "")
            lines.append(f"- {promise['key']}（{promise['status']}，已兑现 {fulfilled_count} 次）" + (f"：{note}" if note else ""))
    if character_states:
        lines.append("人物状态台账（watch 判断依据）：")
        lines.extend(f"- {state['key']}：{json.dumps(state['value'], ensure_ascii=False)[:200]}" for state in character_states)
    if evidence:
        lines.append("检索证据片段：")
        lines.extend(f"- {fragment}" for fragment in evidence)
    lines.append(_CONTRACT_INSTRUCTION)
    lines.append(JSON_OUTPUT_INSTRUCTION)

    input_tokens = output_tokens = used_tokens = 0
    try:
        output, (input_tokens, output_tokens, used_tokens) = await stream_model_json(
            context, system_prompt=prompt_for_role("promise_keeper"), user_prompt="\n".join(lines)
        )
        payload = _normalize_contract(output)
    except Exception:
        # 模型/解析失败降级为空契约：不注入（render 见 degraded 标记），链路不退化。
        logger.warning("promise contract model call failed run=%s", run.get("id"), exc_info=True)
        payload = {"summary": "承诺契约不可用（模型调用失败，已降级）", "due": [], "plant": [], "watch": [], "degraded": True}
    return RoleResult(
        artifact_type="report",
        payload=payload,
        used_tokens=used_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


# ---------------------------------------------------------------------------
# promise_verify：终稿 × due 清单逐条核对 → best-effort 打勾落库
# ---------------------------------------------------------------------------


def _transition_status(row: StoryBibleEntryModel, target: str, now: datetime) -> None:
    """按 entities.py PROMISE_TRANSITIONS 校验推进承诺状态（不 bump version）。"""
    allowed = PROMISE_TRANSITIONS.get(row.status, ())
    if target not in allowed:
        raise ValueError(f"invalid promise transition {row.status} -> {target}")
    row.status = target
    row.updated_at = now


def apply_fulfillment(
    row: StoryBibleEntryModel,
    *,
    chapter_no: int | None,
    quote: str,
    now: datetime,
    anchor: dict[str, object] | None = None,
) -> str:
    """追加一次兑现记录并按状态机打勾；返回兑现后的状态。

    同章节同引文不重复计数（重跑幂等）；每次兑现 version+1。累计次数 ≥
    required_fulfillments（默认 1）时 open→developing→resolved（打勾，
    resolved_chapter 记最新兑现章）；未达次数时 open 推进到 developing。

    anchor（可选，locate_anchor 产出）：兑现证据的段落锚点，落库时补
    paragraph_id/content_hash 两个键——定点改写后按 content_hash 判断
    evidence 引用是否失效（第 11 项引用对账）。找不到锚点时不加键，
    旧格式 {chapter, quote} 完全兼容。
    """
    try:
        value = json.loads(row.value_json or "{}")
    except ValueError:
        value = {}
    fulfillments = [dict(item) for item in value.get("fulfillments") or [] if isinstance(item, dict)]
    if not any(item.get("chapter") == chapter_no and item.get("quote") == quote for item in fulfillments):
        fulfillment: dict[str, object] = {"chapter": chapter_no, "quote": quote}
        if anchor:
            for anchor_key in ("paragraph_id", "content_hash"):
                if anchor.get(anchor_key):
                    fulfillment[anchor_key] = anchor[anchor_key]
        fulfillments.append(fulfillment)
    value["fulfillments"] = fulfillments
    try:
        required = int(value.get("required_fulfillments") or 1)
    except (TypeError, ValueError):
        required = 1
    if len(fulfillments) >= required and row.status in ("open", "developing"):
        if row.status == "open":
            _transition_status(row, "developing", now)
        value["resolved_chapter"] = chapter_no
        _transition_status(row, "resolved", now)
    elif row.status == "open":
        _transition_status(row, "developing", now)
    row.value_json = json.dumps(value, ensure_ascii=False)
    row.version = int(row.version) + 1
    row.updated_at = now
    return row.status


def _normalize_verdicts(output: dict[str, Any], due_keys: set[str]) -> list[dict[str, Any]]:
    """模型判定归一：只留 due 清单内的 key，防止幻觉 key 打到台账上。"""
    verdicts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in output.get("verdicts") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key or key not in due_keys or key in seen:
            continue
        seen.add(key)
        verdicts.append({"key": key, "fulfilled": bool(item.get("fulfilled")), "quote": str(item.get("quote") or "")[:500]})
    return verdicts


def _due_query(item: dict[str, Any]) -> str:
    """due 条目 → 取证查询串：key + reason + evidence 拼合压白截断。"""
    parts = [str(item.get(part) or "").strip() for part in ("key", "reason", "evidence")]
    return " ".join(part for part in parts if part)[:300]


def _scan_chapter_paragraphs(content: str, query: str, *, entities: list[str]) -> list[str]:
    """本章直查分段扫描（确定性、不调模型）：查询词给段落打分取 top，保持原文顺序。

    本章草稿不在 RAG 索引里（索引在写回时才入队），所以本章兑现证据只能
    直查终稿原文；复用 search 的 query_terms/keyword_score 公开打分函数。
    """
    from proseforge.application.retrieval.search import keyword_score, query_terms

    terms = query_terms(query)
    if not terms and not entities:
        return []
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n+", content) if paragraph.strip()]
    scored = [(keyword_score(paragraph, terms, entities), index, paragraph) for index, paragraph in enumerate(paragraphs)]
    scored = [entry for entry in scored if entry[0] > 0]
    scored.sort(key=lambda entry: entry[0], reverse=True)
    picked = sorted(scored[:VERIFY_DIRECT_SCAN_MAX_PARAGRAPHS], key=lambda entry: entry[1])
    selected: list[str] = []
    total_chars = 0
    for _score, _index, paragraph in picked:
        if total_chars + len(paragraph) > VERIFY_DIRECT_SCAN_MAX_CHARS:
            break
        selected.append(paragraph)
        total_chars += len(paragraph)
    return selected


async def _gather_verify_evidence(
    session,
    uow_factory: Any,
    *,
    project_id: str,
    owner_id: str,
    due: list[dict[str, Any]],
    final_content: str,
) -> dict[str, dict[str, list[str]]]:
    """逐条 due 承诺定向取证：RAG 历史证据片段（承诺→段落）+ 本章直查相关段落。

    索引异常向上抛（调用方显式回落旧路径）；单条 legs 失败由 _rag_evidence
    内部吞掉退化为空片段，不在这里区分。
    """
    evidence: dict[str, dict[str, list[str]]] = {}
    for item in due:
        key = str(item["key"]).strip()
        query = _due_query(item)
        passages = await _rag_evidence(session, uow_factory, project_id=project_id, owner_id=owner_id, query=query, entities=[key])
        paragraphs = _scan_chapter_paragraphs(final_content, query, entities=[key])
        evidence[key] = {"rag": passages, "paragraphs": paragraphs}
    return evidence


async def _run_verify(context: TaskContext) -> RoleResult:
    run, task, project_id, artifact_ids = _context_snapshot(context)
    goal = str(run.get("goal") or "")
    chapter_no = parse_chapter_no(goal)
    uow_factory = context.get("uow_factory")
    final: dict[str, Any] | None = None
    contract: dict[str, Any] | None = None
    if uow_factory is not None:
        try:
            async with uow_factory() as uow:  # type: ignore[operator]
                final = await _load_final_draft(uow.session, artifact_ids)
                contract_ids = [
                    str(item.get("id", ""))
                    for item in context.get("artifacts", [])
                    if isinstance(item, dict) and str(item.get("task_key", "")) == "promise_contract" and item.get("id")
                ]
                contract = await _load_contract_payload(uow.session, contract_ids)
        except Exception:
            logger.warning("promise verify snapshot failed run=%s", run.get("id"), exc_info=True)
            final, contract = None, None
    due = _due_items(contract) if contract is not None else []
    if final is None or not due:
        # 无终稿或无到期承诺：核对无事可做，artifact 记明原因（非降级失败）。
        reason = "终稿缺失" if final is None else "契约卡无到期承诺"
        return RoleResult(artifact_type="report", payload={"summary": f"{reason}，跳过承诺核对", "verdicts": []})

    input_budget = context.get("input_budget")
    text_budget = max(2000, int(input_budget) // 2) if input_budget else FINAL_DRAFT_FALLBACK_MAX_CHARS
    final_content = str(final["content"])

    # RAG 定向取证（开关默认 off 走旧路）：每条 due 承诺「承诺→段落」检索历史
    # 证据 + 本章直查分段扫描；取证为空/索引异常显式回落全章通读旧路 + 审计事件。
    from proseforge.settings import get_settings

    targeted: dict[str, dict[str, list[str]]] | None = None
    fallback_reason: str | None = None
    if get_settings().promise_rag_verify and project_id and uow_factory is not None:
        try:
            async with uow_factory() as uow:  # type: ignore[operator]
                owner_id = str(await uow.session.scalar(select(AgentRunModel.user_id).where(AgentRunModel.id == str(run["id"]))) or "")
                targeted = await _gather_verify_evidence(
                    uow.session, uow_factory,
                    project_id=project_id, owner_id=owner_id, due=due, final_content=final_content,
                )
        except Exception:
            # 索引异常绝不影响 verify 出结果：回落旧路径，事件记 index_error。
            logger.warning("promise verify RAG evidence failed run=%s", run.get("id"), exc_info=True)
            targeted, fallback_reason = None, "index_error"
        if targeted is not None and any(not bundle["rag"] and not bundle["paragraphs"] for bundle in targeted.values()):
            # 任一条目两路取证皆空：定向取证无判定依据，整体回落旧路径（命脉不接单条腿）。
            targeted, fallback_reason = None, "no_evidence"

    verify_events: list[dict[str, object]] = []
    if targeted is not None:
        verify_events.append({
            "event": "chapter.promise_rag_verify", "chapter": chapter_no, "due_count": len(due),
            "rag_passages": sum(len(bundle["rag"]) for bundle in targeted.values()),
            "scanned_paragraphs": sum(len(bundle["paragraphs"]) for bundle in targeted.values()),
        })
    elif fallback_reason is not None:
        verify_events.append({"event": "chapter.promise_rag_verify_fallback", "chapter": chapter_no, "reason": fallback_reason})

    lines = [
        f"任务：{task.get('task_key', '')}（角色 promise_keeper）",
        f"写作目标摘要：{goal_hint_for(run)}",
        "本章承诺契约 due 清单：",
        *[f"- {item['key']}：{item.get('reason') or ''!s}" for item in due],
    ]
    # 记忆优先（先查再想）：兑现判定参照已批准记忆（人物状态/时间线等）。
    lines.extend(render_memory_lines([item for item in run.get("memory_slice") or [] if isinstance(item, dict)]))
    if targeted is not None:
        lines.append("承诺定向取证证据（按承诺分组：历史章节检索片段 + 本章终稿相关段落；兑现判定以本章相关段落为准，历史片段只用于理解承诺含义）：")
        for item in due:
            key = str(item["key"]).strip()
            bundle = targeted[key]
            lines.append(f"承诺「{key}」：")
            if bundle["rag"]:
                lines.append("  历史埋设/进展证据：")
                lines.extend(f"  - {passage}" for passage in bundle["rag"])
            if bundle["paragraphs"]:
                lines.append("  本章相关段落：")
                lines.extend(f"  - {paragraph}" for paragraph in bundle["paragraphs"])
    else:
        lines += [
            "终稿正文：",
            elide_middle(final_content, text_budget),
        ]
    lines += [_VERIFY_INSTRUCTION, JSON_OUTPUT_INSTRUCTION]
    input_tokens = output_tokens = used_tokens = 0
    try:
        output, (input_tokens, output_tokens, used_tokens) = await stream_model_json(
            context, system_prompt=prompt_for_role("promise_keeper"), user_prompt="\n".join(lines)
        )
    except Exception:
        # 模型失败：不打勾、不漏报，降级 artifact 记明，链路不退化；
        # 取证/回落事件照常落审计（降级绝不静默）。
        logger.warning("promise verify model call failed run=%s", run.get("id"), exc_info=True)
        return RoleResult(
            artifact_type="report",
            payload={"summary": "承诺核对不可用（模型调用失败，已降级）", "verdicts": [], "degraded": True},
            extra_events=verify_events,
        )

    due_keys = {str(item["key"]).strip() for item in due}
    verdicts = _normalize_verdicts(output, due_keys)
    quote_by_key = {verdict["key"]: verdict["quote"] for verdict in verdicts if verdict["fulfilled"]}
    resolved: list[str] = []
    fulfilled: list[str] = []
    if quote_by_key and project_id and uow_factory is not None:
        now = datetime.now(UTC)
        try:
            # 兑现 evidence 顺手补段落锚点（paragraph_id/content_hash）：
            # 引文定位在终稿分段上，与写回时 chapter_versions.paragraph_anchors
            # 同套切分/哈希，改段后即可按 content_hash 对账失效引用。
            from proseforge.domain.chapter.paragraphs import (
                locate_anchor,
                split_paragraphs,
            )

            final_paragraphs, _separators = split_paragraphs(final_content)
            async with uow_factory() as uow:  # type: ignore[operator]
                rows = await uow.session.scalars(
                    select(StoryBibleEntryModel).where(
                        StoryBibleEntryModel.project_id == project_id,
                        StoryBibleEntryModel.kind == "promise",
                        StoryBibleEntryModel.key.in_(list(quote_by_key)),
                        StoryBibleEntryModel.status.in_(("open", "developing")),
                    )
                )
                for row in rows:
                    status = apply_fulfillment(
                        row, chapter_no=chapter_no, quote=quote_by_key[row.key], now=now,
                        anchor=locate_anchor(final_paragraphs, quote_by_key[row.key]),
                    )
                    fulfilled.append(row.key)
                    if status == "resolved":
                        resolved.append(row.key)
                await uow.commit()
        except Exception:
            # 落库 best-effort：失败不拖垮任务，misssed/未兑现语义不受影响。
            logger.warning("promise verify fulfillment write failed run=%s", run.get("id"), exc_info=True)
            fulfilled, resolved = [], []
    # 未兑现（含模型漏判）记 advisory 审计事件，不进门禁 reason。
    extra_events = [
        {"event": "chapter.promise_missed", "key": key, "chapter": chapter_no}
        for key in sorted(due_keys - set(fulfilled))
    ]
    return RoleResult(
        artifact_type="report",
        payload={
            "summary": str(output.get("summary") or f"承诺核对：兑现 {len(fulfilled)} 条，打勾 {len(resolved)} 条，未兑现 {len(extra_events)} 条"),
            "verdicts": verdicts,
            "fulfilled": fulfilled,
            "resolved": resolved,
        },
        used_tokens=used_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        extra_events=[*verify_events, *extra_events],
    )


# ---------------------------------------------------------------------------
# promise_register：终稿 + goal 钩子行 → 新承诺登记（语义判重）
# ---------------------------------------------------------------------------


def _normalize_new_promises(output: dict[str, Any]) -> list[dict[str, Any]]:
    """登记清单归一：key 非空、category 落入五类（未知归「伏笔」）、批次内去重。"""
    promises: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in output.get("promises") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        category = str(item.get("category") or "").strip()
        duplicate_of = str(item.get("duplicate_of") or "").strip() or None
        promises.append({
            "key": key,
            "category": category if category in PROMISE_CATEGORIES else "伏笔",
            "note": str(item.get("note") or "")[:500],
            "duplicate_of": duplicate_of,
        })
    return promises


def _merge_into_row(row: StoryBibleEntryModel, *, note: str, category: str, now: datetime) -> None:
    """判重命中并入既有行：note 合并（不删行、不改状态），version+1。"""
    try:
        value = json.loads(row.value_json or "{}")
    except ValueError:
        value = {}
    old_note = str(value.get("note") or "")
    if note and note not in old_note:
        value["note"] = f"{old_note}；{note}" if old_note else note
    if category and not value.get("category"):
        value["category"] = category
    row.value_json = json.dumps(value, ensure_ascii=False)
    row.version = int(row.version) + 1
    row.updated_at = now


async def _run_register(context: TaskContext) -> RoleResult:
    run, task, project_id, artifact_ids = _context_snapshot(context)
    goal = str(run.get("goal") or "")
    chapter_no = parse_chapter_no(goal)
    uow_factory = context.get("uow_factory")
    final: dict[str, Any] | None = None
    ledger_promises: list[dict[str, Any]] = []
    if uow_factory is not None:
        try:
            async with uow_factory() as uow:  # type: ignore[operator]
                final = await _load_final_draft(uow.session, artifact_ids)
                if project_id:
                    ledger_promises, _states = await _load_ledger(uow.session, project_id)
        except Exception:
            logger.warning("promise register snapshot failed run=%s", run.get("id"), exc_info=True)
            final, ledger_promises = None, []
    if final is None:
        return RoleResult(artifact_type="report", payload={"summary": "终稿缺失，跳过承诺登记", "promises": [], "registered": 0, "merged": 0})

    plant_hooks = [clue for action, clue in parse_goal_hooks(goal) if action == "plant"]
    input_budget = context.get("input_budget")
    text_budget = max(2000, int(input_budget) // 2) if input_budget else FINAL_DRAFT_FALLBACK_MAX_CHARS
    lines = [
        f"任务：{task.get('task_key', '')}（角色 promise_keeper）",
        f"写作目标摘要：{goal_hint_for(run)}",
    ]
    # 记忆优先（先查再想）：登记判重参照已批准记忆（人物状态/时间线等）。
    lines.extend(render_memory_lines([item for item in run.get("memory_slice") or [] if isinstance(item, dict)]))
    if plant_hooks:
        lines.append("goal 伏笔/钩子行埋入项（必收，key 用钩子原文）：")
        lines.extend(f"- {clue}" for clue in plant_hooks)
    if ledger_promises:
        lines.append("既有承诺台账（open/developing，判重基准）：")
        lines.extend(f"- {promise['key']}（{promise['status']}）：{promise['value'].get('note') or ''!s}" for promise in ledger_promises)
    lines += [
        "终稿正文：",
        elide_middle(str(final["content"]), text_budget),
        _REGISTER_INSTRUCTION,
        JSON_OUTPUT_INSTRUCTION,
    ]
    input_tokens = output_tokens = used_tokens = 0
    try:
        output, (input_tokens, output_tokens, used_tokens) = await stream_model_json(
            context, system_prompt=prompt_for_role("promise_keeper"), user_prompt="\n".join(lines)
        )
    except Exception:
        logger.warning("promise register model call failed run=%s", run.get("id"), exc_info=True)
        return RoleResult(artifact_type="report", payload={"summary": "承诺登记不可用（模型调用失败，已降级）", "promises": [], "registered": 0, "merged": 0, "degraded": True})

    candidates = _normalize_new_promises(output)
    registered: list[str] = []
    merged: list[str] = []
    if candidates and project_id and uow_factory is not None:
        now = datetime.now(UTC)
        try:
            async with uow_factory() as uow:  # type: ignore[operator]
                existing_rows = (await uow.session.scalars(
                    select(StoryBibleEntryModel).where(
                        StoryBibleEntryModel.project_id == project_id,
                        StoryBibleEntryModel.kind == "promise",
                    )
                )).all()
                by_key = {row.key: row for row in existing_rows}
                for candidate in candidates:
                    # 语义判重（模型给 duplicate_of）+ 确定性 key 完全匹配双保险。
                    target_key = candidate["duplicate_of"] if candidate["duplicate_of"] in by_key else None
                    if target_key is None and candidate["key"] in by_key:
                        target_key = candidate["key"]
                    if target_key is not None:
                        _merge_into_row(by_key[target_key], note=candidate["note"], category=candidate["category"], now=now)
                        merged.append(target_key)
                        continue
                    fact = StoryFact.create(
                        project_id, "promise", candidate["key"],
                        {"note": candidate["note"] or candidate["key"], "category": candidate["category"], "introduced_chapter": chapter_no},
                        source="auto", confidence=0.6,
                    )
                    new_row = StoryBibleEntryModel(
                        id=fact.id, project_id=project_id, kind="promise", key=fact.key,
                        value_json=json.dumps(fact.value, ensure_ascii=False),
                        status="open", confidence=0.6, source="auto", pinned=False,
                        version=1, created_at=now, updated_at=now,
                    )
                    uow.session.add(new_row)
                    by_key[fact.key] = new_row
                    registered.append(fact.key)
                await uow.commit()
        except Exception:
            logger.warning("promise register write failed run=%s", run.get("id"), exc_info=True)
            registered, merged = [], []
    return RoleResult(
        artifact_type="report",
        payload={
            "summary": str(output.get("summary") or f"承诺登记：新建 {len(registered)} 条，并入 {len(merged)} 条"),
            "promises": candidates,
            "registered": registered,
            "merged": merged,
        },
        used_tokens=used_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


@register_role("promise_keeper")
async def promise_keeper_handler(context: TaskContext) -> RoleResult:
    """promise_keeper 入口：按 task_key 分派三节点；未知 task_key 回退默认 handler。"""
    task = context["task"]
    assert isinstance(task, dict)
    task_key = str(task.get("task_key") or "")
    if task_key == "promise_contract":
        return await _run_contract(context)
    if task_key == "promise_verify":
        return await _run_verify(context)
    if task_key == "promise_register":
        return await _run_register(context)
    return await default_role_handler(context)
