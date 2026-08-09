"""米开朗基罗·定点改写（第 11 项）：只改审校标注段落，不再全章重生成。

入口 ``try_pinpoint_rewrite`` 由 chief_handler._rewrite_final_draft 在整章
重写之前调用；返回 None 时调用方走旧的整章重写路径（兜底）。返回 None 的
情形：

- 兜底开关：项目 cluster_config_json 的 ``rewrite_mode == "full"``（整章
  重写开关，角色/项目配置层，不改 settings.py）；
- 无 uow 或无 review_*/recheck 上游 Artifact（纯测试 context 等）；
- 门禁未过原因是全局性的（字数不足/必含线索未命中/复读超标/scene missing），
  定点段落改写解决不了，必须整章重写；
- 审校 findings 没有能定位到段落的引文；
- 模型输出既无合法 rewrites 也无整章 content（交给整章路径重试）。

定点路径流程：
1. 收集标注：review_council 合议产出（rewrite_instructions + 去重 findings，
   带引文）优先；无合议时回退 review_*/recheck Artifact 的原始 findings
   （issues/risks，带 evidence_spans 引文）；引文经
   domain.chapter.paragraphs.locate_quote 定位到段落；
2. 一次模型调用：每个待改段带前后各 1 段上下文（仅参照）+ 本段标注清单，
   输出 {"rewrites": [{"index", "content"}]}；模型回旧版整章契约（只有
   content）时按整章产出接受（graceful degradation），不再二次调用；
3. 拼装：只换被标注段的文本，分隔符原样保留——未标注段落字节级不变；
4. 衔接检查（确定性启发式，不调模型）：空段/与邻段雷同 → 回退原文 +
   审计事件；过短/长度异常/结尾标点丢失 → 记警告但保留；
5. evidence 引用失效对账：被改段 content_hash 变化 → 查承诺台账
   （story_bible_entries kind=promise）value_json.fulfillments 里引用该段
   旧 hash/引文的兑现记录 → 按 quote 在新段落里重锚定（补 paragraph_id/
   content_hash，向后兼容的 JSON 扩展），找不到的落 promise.evidence_stale
   审计事件上报奥莉维亚。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from proseforge.application.agents.role_handlers import RoleResult, TaskContext
from proseforge.domain.chapter.paragraphs import (
    build_anchors,
    content_hash,
    join_paragraphs,
    locate_quote,
    paragraph_id,
    split_paragraphs,
)

logger = logging.getLogger(__name__)

# 邻段上下文注入上限（超出截断），待改段本身全文注入不截断。
NEIGHBOR_MAX_CHARS = 400
# 衔接检查：改写段低于此字数视为可疑（保留但记警告）。
MIN_REWRITTEN_CHARS = 8
# 衔接检查：改写段长度超过原段 4 倍 + 200 字（或绝对值 800）记长度异常。
LENGTH_ANOMALY_RATIO = 4
LENGTH_ANOMALY_SLACK = 200
LENGTH_ANOMALY_ABS = 800
# 句号类结尾标点（衔接检查：原段有结尾标点而改写段丢失 → 警告）。
_TERMINAL_PUNCT = tuple("。！？!?…」』）)\"'")
# 全局性门禁未过原因前缀：定点段落改写解决不了，必须走整章重写。
_GLOBAL_GATE_REASON_PREFIXES = ("字数不足", "必含线索未命中", "复读超标", "scene missing")
# findings 段落引用（review_handlers 挂载）单条 finding 的锚点上限。
MAX_PARAGRAPH_REFS = 8


# ---------------------------------------------------------------------------
# 兜底开关与 findings 收集
# ---------------------------------------------------------------------------


async def _pinpoint_enabled(context: TaskContext) -> bool:
    """整章重写兜底开关：项目 cluster_config_json 的 rewrite_mode == "full"。

    配置缺失/损坏/查询失败一律视为开启定点改写（默认新路径）；"full" 之外
    的值（含缺省）都走定点。
    """
    run = context.get("run")
    uow_factory = context.get("uow_factory")
    if not isinstance(run, dict) or uow_factory is None:
        return True
    project_id = str(run.get("project_id") or "")
    if not project_id:
        return True
    try:
        from sqlalchemy import select

        from proseforge.infrastructure.database.models.project import ProjectModel

        async with uow_factory() as uow:  # type: ignore[operator]
            raw = await uow.session.scalar(
                select(ProjectModel.cluster_config_json).where(ProjectModel.id == project_id)
            )
    except Exception:
        logger.warning("pinpoint rewrite config read failed project=%s", project_id, exc_info=True)
        return True
    if not raw:
        return True
    try:
        config = json.loads(raw)
    except ValueError:
        return True
    if not isinstance(config, dict):
        return True
    return str(config.get("rewrite_mode") or "pinpoint").strip().lower() != "full"


def _is_review_task_key(task_key: str) -> bool:
    """review_*（初审电池）与 recheck（终审）的 findings 都是定点改写的标注来源。"""
    return task_key.startswith("review_") or task_key == "recheck"


def _issues_from_council(council_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """合议产出 → 定点标注清单 [{finding, severity, quotes}]。

    rewrite_instructions 优先（合议排序后的执行清单，instruction 是指令本体）；
    未被指令覆盖的带引文 findings 补充进来（合议去重后的完整问题清单）。
    无引文条目无法定位段落，跳过；全部无法定位时调用方回退原始 findings。
    """
    issues: list[dict[str, Any]] = []
    covered: set[str] = set()
    raw_instructions = council_payload.get("rewrite_instructions")
    for item in raw_instructions if isinstance(raw_instructions, list) else []:
        if not isinstance(item, dict):
            continue
        quotes = [str(quote).strip()[:500] for quote in item.get("evidence") or [] if str(quote).strip()]
        instruction = str(item.get("instruction") or item.get("finding") or "").strip()[:300]
        if not instruction and not quotes:
            continue
        issues.append({
            "finding": instruction or "（无文字描述，按引文定位）",
            "severity": str(item.get("severity") or "medium"),
            "quotes": quotes,
        })
        covered.add(str(item.get("finding") or "").strip())
    raw_findings = council_payload.get("findings")
    for item in raw_findings if isinstance(raw_findings, list) else []:
        if not isinstance(item, dict):
            continue
        finding = str(item.get("finding") or "").strip()[:300]
        if finding in covered:
            continue
        quotes: list[str] = []
        for key in ("evidence",):
            for quote in item.get(key) or []:
                text = str(quote).strip()[:500]
                if text and text not in quotes:
                    quotes.append(text)
        raw_spans = item.get("evidence_spans")
        for span in raw_spans if isinstance(raw_spans, list) else []:
            if isinstance(span, dict):
                text = str(span.get("quote") or "").strip()[:500]
                if text and text not in quotes:
                    quotes.append(text)
        if not finding and not quotes:
            continue
        issues.append({
            "finding": finding or "（无文字描述，按引文定位）",
            "severity": str(item.get("severity") or "medium"),
            "quotes": quotes,
        })
    return issues


async def _collect_issues(context: TaskContext) -> list[dict[str, Any]]:
    """上游标注 → 定点标注清单 [{finding, severity, quotes}]。

    合议优先（约翰逊协作化）：review_council 产出存在且能取到带引文条目时，
    消费合议裁定清单（rewrite_instructions + 去重 findings），冲突语义不再被
    绕过；无合议或合议无可用条目时回退 review_*/recheck 原始 findings。
    纯字符串 finding（无引文）无法定位段落，跳过；全部无法定位时调用方
    回退整章重写。
    """
    from proseforge.application.agents.review_handlers import load_council_payload

    council_payload = await load_council_payload(context)
    if council_payload is not None:
        council_issues = _issues_from_council(council_payload)
        if any(issue["quotes"] for issue in council_issues):
            return council_issues
    artifacts = [item for item in context.get("artifacts", []) if isinstance(item, dict)]
    artifact_ids = [
        str(item["id"])
        for item in artifacts
        if item.get("id") and _is_review_task_key(str(item.get("task_key", "")))
    ]
    uow_factory = context.get("uow_factory")
    if not artifact_ids or uow_factory is None:
        return []
    from sqlalchemy import select

    from proseforge.infrastructure.database.models.agents import AgentArtifactModel

    issues: list[dict[str, Any]] = []
    async with uow_factory() as uow:  # type: ignore[operator]
        rows = await uow.session.scalars(
            select(AgentArtifactModel).where(AgentArtifactModel.id.in_(artifact_ids))
        )
        for row in rows:
            try:
                payload = json.loads(row.payload)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            for key in ("issues", "risks", "findings"):
                raw_items = payload.get(key)
                if not isinstance(raw_items, list):
                    continue
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    quotes = []
                    raw_spans = item.get("evidence_spans")
                    if isinstance(raw_spans, list):
                        for span in raw_spans:
                            if isinstance(span, dict):
                                quote = str(span.get("quote") or "").strip()
                                if quote and quote not in quotes:
                                    quotes.append(quote)
                    finding = str(item.get("finding") or "").strip()[:300]
                    if not finding and not quotes:
                        continue
                    issues.append({
                        "finding": finding or "（无文字描述，按引文定位）",
                        "severity": str(item.get("severity") or "medium"),
                        "quotes": quotes,
                    })
    return issues


def _has_global_gate_reasons(context: TaskContext) -> bool:
    """门禁未过原因含全局性条目（字数/线索/复读）：定点改写解决不了。"""
    for reason in context.get("gate_reasons") or []:
        text = str(reason).strip()
        if text.startswith(_GLOBAL_GATE_REASON_PREFIXES):
            return True
    return False


# ---------------------------------------------------------------------------
# 段落定位与 findings 段落引用（review_handlers 挂载点）
# ---------------------------------------------------------------------------


def locate_targets(issues: list[dict[str, Any]], paragraphs: list[str]) -> dict[int, list[dict[str, Any]]]:
    """标注清单 × 段落列表 → {段落序号: [命中该段的标注]}（序号升序）。"""
    targets: dict[int, list[dict[str, Any]]] = {}
    for issue in issues:
        indices: set[int] = set()
        for quote in issue.get("quotes") or []:
            indices.update(locate_quote(paragraphs, quote))
        for index in sorted(indices):
            targets.setdefault(index, []).append(issue)
    return dict(sorted(targets.items()))


def annotate_findings_paragraphs(findings: list[dict[str, Any]], artifact_texts: dict[str, str]) -> list[dict[str, Any]]:
    """给带引文的 findings 补 paragraph_refs（段落锚点引用），best-effort 原地修改。

    审校报告生成处（review_handlers._run_reviewer）挂载：refs 基于注入评审的
    正文（可能按预算截断），只做审计/展示用途；定点改写以引文重定位为准，
    不直接信这里的序号。
    """
    splits = {
        artifact_id: split_paragraphs(text)[0]
        for artifact_id, text in artifact_texts.items()
        if isinstance(text, str) and text.strip()
    }
    if not splits:
        return findings
    fallback_key = next(iter(splits))
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        paragraphs = splits.get(str(finding.get("target_artifact_id") or "")) or splits[fallback_key]
        refs: list[dict[str, Any]] = []
        for span in finding.get("evidence_spans") or []:
            if not isinstance(span, dict):
                continue
            quote = str(span.get("quote") or "").strip()
            if not quote:
                continue
            for index in locate_quote(paragraphs, quote):
                ref = {"paragraph_id": paragraph_id(index), "index": index, "content_hash": content_hash(paragraphs[index])}
                if ref not in refs:
                    refs.append(ref)
        if refs:
            finding["paragraph_refs"] = refs[:MAX_PARAGRAPH_REFS]
    return findings


# ---------------------------------------------------------------------------
# 衔接检查（确定性启发式，不调模型）
# ---------------------------------------------------------------------------


def _cohesion_check(original: str, rewritten: str, neighbors: list[str]) -> tuple[str, list[str]]:
    """改写段衔接检查：返回 (action, flags)；action=reverted 时调用方保留原文。

    硬回退（会破坏衔接或明显是模型复读上下文）：空段、与相邻段雷同。
    软警告（保留但落审计事件）：过短、长度异常、结尾标点丢失。
    """
    if not rewritten.strip():
        return "reverted", ["empty"]
    if any(rewritten == neighbor.strip() for neighbor in neighbors if neighbor.strip()):
        return "reverted", ["duplicates_neighbor"]
    flags: list[str] = []
    if len(rewritten) < MIN_REWRITTEN_CHARS:
        flags.append("too_short")
    if len(rewritten) > max(len(original) * LENGTH_ANOMALY_RATIO + LENGTH_ANOMALY_SLACK, LENGTH_ANOMALY_ABS):
        flags.append("length_anomaly")
    if original.rstrip().endswith(_TERMINAL_PUNCT) and not rewritten.rstrip().endswith(_TERMINAL_PUNCT):
        flags.append("missing_terminal_punctuation")
    return "kept", flags


# ---------------------------------------------------------------------------
# evidence 引用失效对账（承诺台账 fulfillments ↔ 段落锚点）
# ---------------------------------------------------------------------------


async def reconcile_promise_evidence(
    context: TaskContext,
    *,
    old_paragraphs: list[str],
    new_paragraphs: list[str],
    changed_indices: list[int],
) -> list[dict[str, Any]]:
    """被改段落 content_hash 变化 → 承诺 evidence 引用对账；返回审计事件列表。

    对账规则：
    - fulfillment 带 content_hash（新结构）：旧 hash 命中被改段即失效；
    - 旧结构（只有 {chapter, quote}）：章号匹配且 quote 落在被改段即失效；
    - 失效引用按 quote 在新段落里重锚定（补 paragraph_id/content_hash，
      记 reanchored_from）；找不到的落 promise.evidence_stale 上报奥莉维亚。

    落库 best-effort（与 promise_handlers 同款纪律）：DB 异常只降级为
    promise.evidence_reconcile_failed 事件，绝不让对账拖垮改写任务。
    """
    run = context.get("run")
    uow_factory = context.get("uow_factory")
    if not isinstance(run, dict) or uow_factory is None or not changed_indices:
        return []
    project_id = str(run.get("project_id") or "")
    if not project_id:
        return []
    from proseforge.application.agents.review_target import parse_chapter_no

    chapter_no = parse_chapter_no(str(run.get("goal") or ""))
    changed_old = [
        {"index": index, "hash": content_hash(old_paragraphs[index]), "text": old_paragraphs[index]}
        for index in changed_indices
    ]
    events: list[dict[str, Any]] = []
    try:
        from sqlalchemy import select

        from proseforge.infrastructure.database.models.story_bible import (
            StoryBibleEntryModel,
        )

        async with uow_factory() as uow:  # type: ignore[operator]
            rows = await uow.session.scalars(
                select(StoryBibleEntryModel).where(
                    StoryBibleEntryModel.project_id == project_id,
                    StoryBibleEntryModel.kind == "promise",
                )
            )
            now = datetime.now(UTC)
            dirty = False
            for row in rows:
                try:
                    value = json.loads(row.value_json or "{}")
                except ValueError:
                    continue
                fulfillments = value.get("fulfillments")
                if not isinstance(fulfillments, list):
                    continue
                row_changed = False
                for fulfillment in fulfillments:
                    if not isinstance(fulfillment, dict):
                        continue
                    old_hash = str(fulfillment.get("content_hash") or "")
                    quote = str(fulfillment.get("quote") or "").strip()
                    # 失效判定：新结构按 hash 精确命中；旧结构按章号 + 引文落段。
                    hit = None
                    if old_hash:
                        hit = next((item for item in changed_old if item["hash"] == old_hash), None)
                    if hit is None and quote:
                        if chapter_no is not None and fulfillment.get("chapter") != chapter_no:
                            continue
                        hit = next((item for item in changed_old if locate_quote([item["text"]], quote)), None)
                    if hit is None:
                        continue
                    # 重锚定：按 quote 在新段落里找新锚点。
                    new_hits = locate_quote(new_paragraphs, quote) if quote else []
                    if new_hits:
                        new_index = new_hits[0]
                        fulfillment["paragraph_id"] = paragraph_id(new_index)
                        fulfillment["content_hash"] = content_hash(new_paragraphs[new_index])
                        fulfillment["reanchored_from"] = hit["hash"]
                        row_changed = True
                        events.append({
                            "event": "promise.evidence_reanchored",
                            "key": row.key,
                            "chapter": fulfillment.get("chapter"),
                            "paragraph_id": paragraph_id(new_index),
                            "old_paragraph_id": paragraph_id(int(hit["index"])),
                        })
                    else:
                        events.append({
                            "event": "promise.evidence_stale",
                            "key": row.key,
                            "chapter": fulfillment.get("chapter"),
                            "quote": quote[:200],
                            "stale_paragraph_id": paragraph_id(int(hit["index"])),
                            "stale_hash": str(hit["hash"]),
                        })
                if row_changed:
                    value["fulfillments"] = fulfillments
                    row.value_json = json.dumps(value, ensure_ascii=False)
                    row.version = int(row.version) + 1
                    row.updated_at = now
                    dirty = True
            if dirty:
                await uow.commit()
    except Exception:
        logger.warning("promise evidence reconcile failed run=%s", run.get("id"), exc_info=True)
        events.append({"event": "promise.evidence_reconcile_failed", "changed_paragraphs": changed_indices})
    return events


# ---------------------------------------------------------------------------
# 定点改写主流程
# ---------------------------------------------------------------------------

_PINPOINT_OUTPUT_CONTRACT = (
    "执行定点改写：只改写下面列出的待改段落，其余段落由程序逐字保留，你不负责输出。"
    "每个待改段落带前一段/后一段作为衔接参照（仅供把握文风过渡，不得改动、不得复述进输出），"
    "以及本段的审校标注。逐段落实标注：只改动标注涉及的句段，段落其余文字尽量保留；"
    "保持与前后段的文风、视角、时态自然衔接；保持设定连续，不借机润色未涉问题的内容。"
    '只输出一个 JSON 对象 {"title": "...", "rewrites": [{"index": 待改段落序号整数, "content": "改写后的段落全文"}]}，'
    "rewrites 必须覆盖每个列出的待改段落，index 原样照抄；不要输出 Markdown 代码围栏或任何额外解释。"
)


def _pinpoint_system_prompt() -> str:
    """定点改写系统提示词：chief_editor 人格主体 + 定点输出契约；人格缺失回退身份行。"""
    from proseforge.application.agents.prompts import persona_for_role

    persona = persona_for_role("chief_editor") or "你是改写主编。"
    return f"{persona}\n{_PINPOINT_OUTPUT_CONTRACT}"


def _pinpoint_user_prompt(context: TaskContext, *, paragraphs: list[str], targets: dict[int, list[dict[str, Any]]]) -> str:
    from proseforge.application.agents.artifact_texts import elide_middle
    from proseforge.application.agents.prompts import goal_hint_for

    run = context["run"]
    assert isinstance(run, dict)
    lines = [f"写作目标摘要：{goal_hint_for(run)}"]
    gate_reasons = [str(reason) for reason in context.get("gate_reasons") or [] if str(reason).strip()]
    if gate_reasons:
        # 只剩评审类原因（全局性原因已在入口处拦截走整章重写）。
        lines.append("质量门禁未通过原因（总调度判定，必须逐条解决）：")
        lines.extend(f"- {reason}" for reason in gate_reasons)
    lines.append(f"共 {len(paragraphs)} 段，待改 {len(targets)} 段。逐段如下：")
    for index, issues in targets.items():
        lines.append(f"【待改段落 {paragraph_id(index)}（index={index}）】")
        if index > 0:
            lines.append("前一段（参照，勿改）：" + elide_middle(paragraphs[index - 1], NEIGHBOR_MAX_CHARS))
        lines.append("原文：" + paragraphs[index])
        if index + 1 < len(paragraphs):
            lines.append("后一段（参照，勿改）：" + elide_middle(paragraphs[index + 1], NEIGHBOR_MAX_CHARS))
        lines.append("本段审校标注（逐条落实）：")
        for issue in issues:
            line = f"- [{issue['severity']}] {issue['finding']}"
            if issue.get("quotes"):
                line += "（引文：" + "；".join(str(quote)[:120] for quote in issue["quotes"][:3]) + "）"
            lines.append(line)
    lines.append("按段落序号逐段输出改写结果（rewrites 覆盖每个待改段落）。")
    return "\n".join(lines)


def _parse_rewrites(output: dict[str, Any], targets: dict[int, Any], paragraphs: list[str]) -> dict[int, str]:
    """模型输出 → {段落序号: 改写文本}；只收待改集合内、非空、与原文不同的条目。"""
    rewrites: dict[int, str] = {}
    raw = output.get("rewrites")
    if not isinstance(raw, list):
        return rewrites
    for item in raw:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            # 兼容模型回 paragraph_id（"p0003"）而不是 index。
            slug = str(item.get("paragraph_id") or "")
            if slug.startswith("p") and slug[1:].isdigit():
                index = int(slug[1:])
            else:
                continue
        if index not in targets or index >= len(paragraphs):
            continue
        text = item.get("content")
        if not isinstance(text, str):
            continue
        stripped = text.strip()
        if not stripped or stripped == paragraphs[index].strip():
            continue
        rewrites[index] = stripped
    return rewrites


async def try_pinpoint_rewrite(context: TaskContext, scene: dict[str, Any]) -> RoleResult | None:
    """定点改写入口；返回 None 表示走整章重写兜底（见模块 docstring）。"""
    content = str(scene.get("content") or "")
    if not content.strip():
        return None
    if not await _pinpoint_enabled(context):
        return None
    if _has_global_gate_reasons(context):
        return None
    issues = await _collect_issues(context)
    if not issues:
        return None
    paragraphs, separators = split_paragraphs(content)
    targets = locate_targets(issues, paragraphs)
    if not targets:
        return None

    from proseforge.application.agents.review_handlers import stream_model_json

    user_prompt = _pinpoint_user_prompt(context, paragraphs=paragraphs, targets=targets)
    output, (input_tokens, output_tokens, used_tokens) = await stream_model_json(
        context, system_prompt=_pinpoint_system_prompt(), user_prompt=user_prompt
    )
    title = output.get("title")
    final_title = str(title) if isinstance(title, str) and title.strip() else str(scene.get("title") or "终稿")
    located_issue_count = len({id(issue) for issues in targets.values() for issue in issues})

    rewrites = _parse_rewrites(output, targets, paragraphs)
    if not rewrites:
        # Graceful degradation：模型回旧版整章契约（只有 content）时按整章
        # 产出接受，不二次调用；content 也没有 → None，整章路径重试。
        full_text = output.get("content")
        if isinstance(full_text, str) and full_text.strip():
            return RoleResult(
                artifact_type="candidate",
                payload={
                    "title": final_title,
                    "content": full_text,
                    "rewrite_of": str(scene.get("id", "")),
                    "pinpoint": {"mode": "full_text_fallback", "located_issues": located_issue_count, "total_issues": len(issues)},
                },
                used_tokens=used_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                extra_events=[{"event": "rewrite.pinpoint_fallback", "reason": "model_returned_full_text"}],
            )
        return None

    # 拼装 + 衔接检查：只换被标注段，分隔符原样保留 → 未标注段字节级不变。
    new_paragraphs = list(paragraphs)
    changed_indices: list[int] = []
    reverted: list[int] = []
    cohesion_events: list[dict[str, Any]] = []
    for index, rewritten in sorted(rewrites.items()):
        neighbors = [paragraphs[i] for i in (index - 1, index + 1) if 0 <= i < len(paragraphs)]
        action, flags = _cohesion_check(paragraphs[index], rewritten, neighbors)
        cohesion_events.append({
            "event": "rewrite.cohesion",
            "paragraph_id": paragraph_id(index),
            "index": index,
            "action": action,
            "flags": flags,
        })
        if action == "reverted":
            reverted.append(index)
            continue
        new_paragraphs[index] = rewritten
        changed_indices.append(index)
    if not changed_indices:
        return None  # 全部被衔接检查回退：交给整章重写路径
    new_content = join_paragraphs(new_paragraphs, separators)

    reconcile_events = await reconcile_promise_evidence(
        context,
        old_paragraphs=paragraphs,
        new_paragraphs=new_paragraphs,
        changed_indices=changed_indices,
    )
    old_anchors = build_anchors(content)
    new_anchors = build_anchors(new_content)
    changed_detail = [
        {
            "paragraph_id": paragraph_id(index),
            "index": index,
            "old_hash": str(old_anchors[index]["content_hash"]),
            "new_hash": str(new_anchors[index]["content_hash"]),
        }
        for index in changed_indices
    ]
    extra_events: list[dict[str, Any]] = [
        {
            "event": "rewrite.pinpoint",
            "changed_paragraphs": [paragraph_id(index) for index in changed_indices],
            "reverted_paragraphs": [paragraph_id(index) for index in reverted],
            "located_issues": located_issue_count,
            "total_issues": len(issues),
        },
        *cohesion_events,
        *reconcile_events,
    ]
    return RoleResult(
        artifact_type="candidate",
        payload={
            "title": final_title,
            "content": new_content,
            "rewrite_of": str(scene.get("id", "")),
            "pinpoint": {
                "mode": "pinpoint",
                "changed_paragraphs": changed_detail,
                "reverted_paragraphs": [paragraph_id(index) for index in reverted],
                "located_issues": located_issue_count,
                "total_issues": len(issues),
            },
        },
        used_tokens=used_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        extra_events=extra_events,
    )
