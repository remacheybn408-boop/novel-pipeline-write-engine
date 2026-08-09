"""chief_editor handler（蓝图 V3-007）：MergeCandidate → V2 RevisionProposal，替换 executor 占位路径。

流程：graph 输出 manifest（TaskContext artifacts）+ 本 run 已落库评审 + run.chapter_id/
base_version_id → 先按 merge_editor 同一四桶分类产出 MergeCandidate，再创建 V2
RevisionProposal：after = base.content + "\\n\\n" + 合并附录（模型撰写；输出不可用回退
候选摘要，保证 v3-execution-proposal 链路可达）。

guard gating：存在未裁定（resolution is None）CONFLICT 评审时 proposal.guard_status
置 "blocked"，V2 approve 走 ApprovalBlocked → 422（application/revision/
approve_proposal.py），冲突裁定前不可批准。RevisionRepository.create 不接收
guard_status 参数，在返回行上赋值后随调用方事务一并提交。

run 无 chapter_id/base_version_id 时只产 MergeCandidate，不建 proposal。
独立 revise 意图（上游只有 MergeCandidate、无 SceneDraft）按 run.chapter_id 或
goal 章节号注入章节正文作为改写对象（review_target.resolve_chapter_target），
无法确定改写对象时降级为基于 merge 候选产出新建章节草稿（new_chapter=True），
不再以 FAILED 拖垮整个 run（swarm 入口的空项目拦截是第一道防线）。
proposal 创建幂等：run.proposal_id 已设置且行存在 → 复用，不重复创建/发事件。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from proseforge.application.agents.artifact_texts import elide_middle
from proseforge.application.agents.prompts import (
    goal_hint_for,
    persona_for_role,
    prompt_for_role,
)
from proseforge.application.agents.review_handlers import (
    build_merge_payload,
    snapshot_review,
    stream_model_json,
)
from proseforge.application.agents.review_target import resolve_chapter_target
from proseforge.application.agents.role_handlers import (
    RoleResult,
    TaskContext,
    register_role,
)
from proseforge.domain.agents import policy
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentEventModel,
    AgentReviewModel,
    AgentRunModel,
)
from proseforge.infrastructure.database.models.revision import RevisionProposalModel

_MERGE_BUCKETS: tuple[str, ...] = ("agreements", "conflicts", "unsupported", "accepted")

# Fallback full-text cap when the context carries no input_budget (direct
# handler use in tests; the executor always sets one). Same scale as
# role_handlers.ANALYST_GOAL_FALLBACK_MAX_CHARS.
REWRITE_FALLBACK_MAX_CHARS = 20000

# 改写输出契约（人格主体之外的固定部分，与原 _REWRITE_SYSTEM_PROMPT 契约一致）
# 定点修改纪律：改写是定点手术不是全章重写——只改清单涉及位置，其余逐字保留。
_REWRITE_OUTPUT_CONTRACT = (
    "根据修改清单把场景正文改写为终稿：按修改清单逐条定点修改——只改动清单问题涉及的句段，"
    "其余正文逐字保留（措辞、节奏、段落顺序均不变），不重写、不借机润色未涉问题的段落；"
    "清单条目带引文（quote/evidence_spans）时，按引文定位修改点，改动以引文所在句段为限。"
    "保持设定连续与原有篇幅量级。只输出一个 JSON 对象 {\"title\": \"...\", \"content\": \"...\"}，"
    "不要输出 Markdown 代码围栏或任何额外解释。"
)


def _rewrite_system_prompt() -> str:
    """改写系统提示词：chief_editor 人格文件主体 + 改写输出契约；人格文件缺失回退内置身份行。"""
    persona = persona_for_role("chief_editor") or "你是改写主编。"
    return f"{persona}\n{_REWRITE_OUTPUT_CONTRACT}"


# 新章草稿输出契约（章节不存在时独立 revise 的降级路径）
_DRAFT_NEW_CHAPTER_CONTRACT = (
    "根据写作目标与修改清单撰写一个全新章节的草稿（原章节尚不存在，本次为新建）。"
    "只输出一个 JSON 对象 {\"title\": \"...\", \"content\": \"...\"}，"
    "不要输出 Markdown 代码围栏或任何额外解释。"
)


def _draft_new_chapter_system_prompt() -> str:
    """新章草稿系统提示词：chief_editor 人格文件主体 + 新建章节输出契约。"""
    persona = persona_for_role("chief_editor") or "你是改写主编。"
    return f"{persona}\n{_DRAFT_NEW_CHAPTER_CONTRACT}"


async def _draft_new_chapter(context: TaskContext, merge_payload: dict[str, Any], *, reason: str) -> RoleResult:
    """Standalone revise fallback: the target chapter does not exist, so
    instead of failing the run, draft a brand-new chapter from the run goal
    plus the MergeCandidate buckets. The payload marks the chapter as newly
    created (new_chapter=True) and carries the resolution reason as a note.
    """
    run = context["run"]
    assert isinstance(run, dict)
    input_budget = context.get("input_budget")
    text_budget = max(2000, int(input_budget) // 2) if input_budget else REWRITE_FALLBACK_MAX_CHARS
    merge_text = json.dumps({key: merge_payload.get(key, []) for key in _MERGE_BUCKETS}, ensure_ascii=False)
    lines = [
        f"写作目标摘要：{goal_hint_for(run)}",
        "修改清单（四桶分类，可为空）：",
        elide_middle(merge_text, min(len(merge_text), max(1000, text_budget // 4))),
    ]
    output, (input_tokens, output_tokens, used_tokens) = await stream_model_json(
        context, system_prompt=_draft_new_chapter_system_prompt(), user_prompt="\n".join(lines)
    )
    content = output.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("chief_editor new-chapter draft returned empty content")
    title = output.get("title")
    return RoleResult(
        artifact_type="candidate",
        payload={
            "title": str(title) if isinstance(title, str) and title.strip() else "新建章节草稿",
            "content": content,
            "new_chapter": True,
            "note": f"原改写对象不存在（{reason}），本次产出为新建章节草稿。",
        },
        used_tokens=used_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def _rewrite_final_draft(context: TaskContext, scene: dict[str, Any], merge_payload: dict[str, Any]) -> RoleResult:
    """Pipeline rewrite mode: SceneDraft full text + 修改清单 -> final
    {"title","content"} draft (rewrite_of links the source scene). 修改清单
    优先取 review_council 合议裁定清单，无合议时回退 MergeCandidate 四桶。

    篇幅硬要求：与质量门禁同一阈值（quality_gate.parse_min_words，取自 run
    goal 的显式目标行/区间/限定词单值，默认 2500）。首轮产出不足时自动
    扩写重试一轮（取较长者）；仍不足不 FAIL 整章（避免批量断章），以较
    长者产出并附 chapter.length_shortfall 审计事件。
    """
    from proseforge.application.agents.quality_gate import parse_min_words

    run = context["run"]
    assert isinstance(run, dict)
    # 定点改写（第 11 项）：审校标注可定位到段落时只改标注段（其余段落字节级
    # 不变）；返回 None（兜底开关/无可定位标注/全局性门禁原因/输出不可用）
    # 走下方整章重写旧路径。
    from proseforge.application.agents.pinpoint_rewrite import try_pinpoint_rewrite

    pinpointed = await try_pinpoint_rewrite(context, scene)
    if pinpointed is not None:
        return pinpointed
    min_words = parse_min_words(goal_hint_for(run))
    # Budget-trim the two big prompt parts against the per-role input budget
    # (the executor always sets one): an untrimmed scene full text plus the
    # full four-bucket list could overflow the model window and fail the
    # whole run after retries. Half the token budget in chars (CJK ~chars/2
    # tokens, same estimate as role_handlers) leaves headroom for the
    # persona system prompt and the rewritten output; oversized parts are
    # elided head 70% / tail 20% (artifact_texts.elide_middle).
    input_budget = context.get("input_budget")
    text_budget = max(2000, int(input_budget) // 2) if input_budget else REWRITE_FALLBACK_MAX_CHARS
    from proseforge.application.work.retriever import render_pack_text

    pack_sections = context.get("scene_pack")
    pack_text = render_pack_text(pack_sections) if pack_sections else None
    # 合议优先（约翰逊协作化）：review_council 产出存在时，修改清单 = 合议
    # 裁定清单（rewrite_instructions + rulings + 去重 findings），冲突语义
    # 不再被四桶绕过；无合议（独立 revise / 旧图）回退四桶 JSON。
    from proseforge.application.agents.review_handlers import load_council_payload

    council_payload = await load_council_payload(context)
    if council_payload is not None:
        directives = {
            "rewrite_instructions": council_payload.get("rewrite_instructions", []),
            "rulings": council_payload.get("rulings", []),
            "findings": council_payload.get("findings", []),
        }
        merge_text = json.dumps(directives, ensure_ascii=False)
        list_label = (
            "修改清单（评审合议裁定清单：rewrite_instructions 逐条定点落实——只修改清单涉及的位置，"
            "其余正文逐字保留；带 evidence 引文的条目按引文定位；rulings 是合议对评审冲突的裁定，"
            "按其 winner_role 一方口径落实；findings 为合议去重后的完整问题清单）："
        )
    else:
        merge_text = json.dumps({key: merge_payload.get(key, []) for key in _MERGE_BUCKETS}, ensure_ascii=False)
        list_label = "修改清单（四桶分类；conflicts/unsupported 逐条定点落实——只修改清单涉及的位置，其余正文逐字保留；带引文的条目按引文定位；agreements 保持）："
    merge_chars = min(len(merge_text), max(1000, text_budget // 4))
    pack_chars = min(len(pack_text), max(800, text_budget // 4)) if pack_text else 0
    scene_chars = max(1000, text_budget - merge_chars - pack_chars)
    lines = [
        f"写作目标摘要：{goal_hint_for(run)}",
    ]
    # 硬事实卡：全书大纲的数量/年代/专名，定点修改时不得因改写引入事实漂移。
    from proseforge.application.agents.hard_facts import render_hard_fact_card

    fact_card = render_hard_fact_card(str(run.get("goal") or ""))
    if fact_card:
        lines.append(fact_card)
    # 本章承诺契约卡（奥莉维亚 promise_contract 产出）：改写是 due/plant 落实
    # 的最后一道机会，与硬事实卡同款注入；artifact 缺失/降级不注入。
    from proseforge.application.agents.promise_handlers import (
        load_promise_contract_card,
    )

    contract_card = await load_promise_contract_card(context)
    if contract_card:
        lines.append(
            "【本章承诺契约】改写不得丢失以下承诺约束：due 条目已兑现的正文保留兑现情节，"
            "未兑现的本次必须落实；plant 钩子保留为具体细节：\n" + contract_card
        )
    # 记忆优先（先查再想）：改写不得与已批准记忆（人物状态/道具/时间线）冲突。
    from proseforge.application.agents.prompts import render_memory_lines

    memory_slice = [item for item in run.get("memory_slice") or [] if isinstance(item, dict)]
    lines.extend(render_memory_lines(memory_slice))
    # 跨章接缝卡 + 场景衔接卡：改写不得改断章间接缝、不得磨掉接力句。
    from proseforge.application.agents.role_handlers import _load_scene_bridge
    from proseforge.application.agents.seam_card import load_seam_card

    seam_card, seam_lagging = await load_seam_card(context, str(run.get("goal") or ""))
    if seam_card:
        lines.append(seam_card + "\n（改写约束：以上接缝锚点必须保住，本章开头不得改断。）")
    bridge_card = await _load_scene_bridge(context)
    if bridge_card:
        lines.append("【场景衔接卡】本章写作侧衔接规划（改写须守住接力句）：\n" + bridge_card)
    gate_reasons = [str(reason) for reason in context.get("gate_reasons") or [] if str(reason).strip()]
    if gate_reasons:
        # 质量门禁未过原因（总调度判定）：改写必须逐条解决，这是本次改写的起因。
        lines.append("质量门禁未通过原因（总调度判定，必须逐条解决）：")
        lines.extend(f"- {reason}" for reason in gate_reasons)
    if pack_text:
        # Same injection point as build_task_prompt / _compose_appendix: the
        # narrative-RAG pack carries 未结伏笔与前文片段，改写落实伏笔靠它。
        lines.extend(["叙事检索场景包（含未结伏笔清单与前文片段）：", elide_middle(pack_text, pack_chars)])
    lines += [
        "待改写正文：",
        elide_middle(str(scene.get("content", "")), scene_chars),
        list_label,
        elide_middle(merge_text, merge_chars),
        (
            "伏笔要求：场景包「未结伏笔」中约定本章回收的，终稿必须正面回收；"
            "约定本章埋入的钩子，终稿必须落实为具体细节；不得为收伏笔而硬转折，回收要长在情节里。"
        ),
        f"篇幅硬要求：正文 content 不得少于 {min_words} 字，不足即不合格，请充分铺陈后再收尾。",
    ]
    output, (input_tokens, output_tokens, used_tokens) = await stream_model_json(
        context, system_prompt=_rewrite_system_prompt(), user_prompt="\n".join(lines)
    )
    content = output.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("chief_editor rewrite returned empty content")
    title = output.get("title")
    if len(content.strip()) < min_words:
        # 扩写重试：保留评审落实，明确上次字数缺口，要求扩写达标。
        retry_lines = lines + [
            (
                f"上次产出仅 {len(content.strip())} 字，未达到不少于 {min_words} 字的硬要求。"
                "请保留对修改清单的落实，充分铺陈场景、动作、对话与心理描写，扩写到达标篇幅。"
            )
        ]
        retry_output, (retry_input, retry_output_tokens, retry_used) = await stream_model_json(
            context, system_prompt=_rewrite_system_prompt(), user_prompt="\n".join(retry_lines)
        )
        input_tokens += retry_input
        output_tokens += retry_output_tokens
        used_tokens += retry_used
        retry_content = retry_output.get("content")
        if isinstance(retry_content, str) and len(retry_content.strip()) > len(content.strip()):
            content = retry_content
            retry_title = retry_output.get("title")
            if isinstance(retry_title, str) and retry_title.strip():
                title = retry_title
    extra_events: list[dict[str, Any]] = []
    if memory_slice:
        # 记忆优先审计：本任务实际看到的已批准记忆条数。
        extra_events.append({"event": "memory.seen", "count": len(memory_slice)})
    if seam_lagging:
        # 前章 L0 摘要尚未落库（异步摘要链路延迟）：不阻塞，只留可见性。
        extra_events.append({"event": "context.summary_lagging", "role": "chief_editor"})
    if len(content.strip()) < min_words:
        # 仍不达标：不 FAIL（批量链不断章），产出较长者并留审计事件。
        extra_events.append({"event": "chapter.length_shortfall", "min_words": min_words, "actual": len(content.strip())})
    return RoleResult(
        artifact_type="candidate",
        payload={
            "title": str(title) if isinstance(title, str) and title.strip() else str(scene.get("title") or "终稿"),
            "content": content,
            "rewrite_of": str(scene.get("id", "")),
        },
        used_tokens=used_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        extra_events=extra_events,
    )


def unresolved_conflict_groups(reviews: list[dict[str, Any]]) -> list[str]:
    """未裁定（resolution is None）CONFLICT 评审的 conflict_group（去重升序）。"""
    return sorted({str(review["conflict_group"]) for review in reviews if review["status"] == "CONFLICT" and review["conflict_group"] and review["resolution"] is None})


def fallback_appendix(merge_payload: dict[str, Any]) -> str:
    """模型输出不可用时的确定性附录：候选摘要 + 桶计数（保证提案链路可达）。"""
    counts = ", ".join(f"{key}={len(merge_payload.get(key, []))}" for key in _MERGE_BUCKETS)
    return f"{merge_payload.get('summary', '')}\n[MergeCandidate] {counts}".strip()


async def _lock_run(uow, run_id: str) -> AgentRunModel:
    locked = await uow.session.scalar(
        select(AgentRunModel).where(AgentRunModel.id == run_id).with_for_update().execution_options(populate_existing=True)
    )
    if locked is None:
        raise LookupError("agent run not found")
    return locked


def _append_event(uow, run: AgentRunModel, event_type: str, data: dict[str, Any]) -> None:
    """在调用方事务内追加 run 事件（调用方已持有 run 行锁，sequence 单调递增）。"""
    sequence = int(run.event_cursor) + 1
    uow.session.add(AgentEventModel(id=new_id(), run_id=run.id, sequence=sequence, event_type=event_type, payload=json.dumps(data, ensure_ascii=False, sort_keys=True)))
    run.event_cursor = sequence
    run.updated_at = datetime.now(UTC)


async def create_chief_proposal(uow, run_id: str, reviews: list[dict[str, Any]], *, appendix: str) -> dict[str, Any]:
    """在调用方事务内创建（或幂等复用）V2 RevisionProposal；不自行 commit。

    返回 {"proposal_id", "guard_status", "created"}；base version 缺失抛 LookupError。
    """
    locked = await _lock_run(uow, run_id)
    if locked.proposal_id:
        existing = await uow.session.get(RevisionProposalModel, locked.proposal_id)
        if existing is not None:
            return {"proposal_id": existing.id, "guard_status": existing.guard_status, "created": False}
    base = await uow.chapters.get_version_owned(str(locked.chapter_id), str(locked.base_version_id), str(locked.user_id))
    if base is None:
        raise LookupError("chief editor base version not found")
    blocked = unresolved_conflict_groups(reviews)
    proposal = await uow.revisions.create(
        chapter_id=str(locked.chapter_id),
        base_version_id=base.id,
        before=base.content,
        after=f"{base.content}\n\n{appendix}",
        rationale=f"Chief Editor merge of agent run {run_id}: {len(reviews)} reviews, {len(blocked)} unresolved conflict group.",
    )
    # revisions.create 不接收 guard_status：在返回行上赋值，随调用方事务一并提交
    proposal.guard_status = "blocked" if blocked else "clear"
    locked.proposal_id = proposal.id
    _append_event(uow, locked, "proposal.created", {"proposal_id": proposal.id, "guard_status": proposal.guard_status})
    if blocked:
        _append_event(uow, locked, "proposal.blocked", {"proposal_id": proposal.id, "conflict_groups": blocked})
    return {"proposal_id": proposal.id, "guard_status": proposal.guard_status, "created": True}


async def run_chief_proposal(uow, run_id: str, reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """端点路径（POST .../chief-proposal）：无 run 凭据可用，附录用确定性回退；在端点事务内执行。"""
    return await create_chief_proposal(uow, run_id, reviews, appendix=fallback_appendix(build_merge_payload(reviews)))


async def _compose_appendix(context: TaskContext, merge_payload: dict[str, Any]) -> tuple[str, tuple[int, int, int]]:
    """模型撰写合并附录；输出不可用（异常/非 JSON/缺 appendix）回退候选摘要，不阻断提案链路。"""
    task = context["task"]
    run = context["run"]
    assert isinstance(task, dict) and isinstance(run, dict)
    artifacts = [item for item in context.get("artifacts", []) if isinstance(item, dict)]
    from proseforge.application.work.retriever import render_pack_text

    pack_sections = context.get("scene_pack")
    pack_text = render_pack_text(pack_sections) if pack_sections else None
    lines = [
        f"任务：{task.get('task_key', '')}（角色 chief_editor）",
        f"写作目标摘要：{goal_hint_for(run)}",
    ]
    if pack_text:
        # Same injection point as build_task_prompt: before the artifacts.
        lines.extend(["叙事检索场景包：", pack_text])
    lines += [
        "graph 输出 manifest：",
        *[f"- artifact_id={item.get('id', '')} [{item.get('artifact_type', '')}] {item.get('task_key', '')}: {item.get('preview', '')}" for item in artifacts],
        f"一致发现（agreements）：{json.dumps(merge_payload.get('agreements', []), ensure_ascii=False)[:2000]}",
        f"已接受发现（accepted）：{json.dumps(merge_payload.get('accepted', []), ensure_ascii=False)[:2000]}",
        "撰写追加在正文后的合并附录（appendix），落实一致与已接受发现；不得改写原文。",
    ]
    try:
        output, tokens = await stream_model_json(context, system_prompt=prompt_for_role("chief_editor"), user_prompt="\n".join(lines))
    except Exception:
        return fallback_appendix(merge_payload), (0, 0, 0)
    appendix = output.get("appendix")
    if not isinstance(appendix, str) or not appendix.strip():
        return fallback_appendix(merge_payload), tokens
    return appendix.strip(), tokens


@register_role("chief_editor")
async def chief_editor_handler(context: TaskContext) -> RoleResult:
    """chief_editor：MergeCandidate → V2 RevisionProposal（guard 阻塞语义见模块 docstring）。"""
    policy.authorize("chief_editor", "create_artifact")
    policy.authorize("chief_editor", "create_revision_proposal")
    run = context["run"]
    assert isinstance(run, dict)
    run_id = str(run["id"])
    uow_factory = context["uow_factory"]
    assert callable(uow_factory)

    # 读阶段：短事务内快照 run 行与评审（会话关闭后 ORM 实例过期，后续只用快照 dict）
    async with uow_factory() as uow:
        row = await uow.session.get(AgentRunModel, run_id)
        if row is None:
            raise LookupError("agent run not found")
        chapter_id, base_version_id = row.chapter_id, row.base_version_id
        reviews = [snapshot_review(item) for item in await uow.session.scalars(select(AgentReviewModel).where(AgentReviewModel.run_id == run_id))]
        # Pipeline分流: load upstream artifact payloads in the same short
        # transaction. SceneDraft (non-empty content) + MergeCandidate
        # (four-bucket payload) both present -> rewrite mode.
        artifact_ids = [str(item.get("id", "")) for item in context.get("artifacts", []) if isinstance(item, dict) and item.get("id")]
        scene: dict[str, Any] | None = None
        merge_candidate: dict[str, Any] | None = None
        if artifact_ids:
            # Ordered by id (time-ordered new_id): with bounded revise
            # rounds, the LATEST final-lineage artifact (select winner or
            # the previous round's rewrite) is the rewrite base — round 2
            # improves the round-1 final draft instead of starting over.
            for artifact_row in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.id.in_(artifact_ids)).order_by(AgentArtifactModel.id)):
                try:
                    artifact_payload = json.loads(artifact_row.payload)
                except ValueError:
                    continue
                if not isinstance(artifact_payload, dict):
                    continue
                # Pipeline select output (three-draft winner, marked with
                # "selected_from") and previous-round rewrites ("rewrite_of")
                # outrank the raw parallel scene drafts.
                if isinstance(artifact_payload.get("content"), str) and artifact_payload["content"].strip():
                    if artifact_payload.get("selected_from") or artifact_payload.get("rewrite_of") or scene is None:
                        scene = {"id": artifact_row.id, "title": artifact_payload.get("title"), "content": artifact_payload["content"]}
                elif "agreements" in artifact_payload:
                    # Latest MergeCandidate wins (revise rounds re-run merge).
                    merge_candidate = artifact_payload
    if scene is None and merge_candidate is not None and not (chapter_id and base_version_id):
        # Standalone revise intent: the graph has no scene draft upstream, so
        # inject the chapter full text (run.chapter_id or the goal's chapter
        # number) as the rewrite target. When no target exists (e.g. the
        # chapter was never created), degrade to drafting a NEW chapter from
        # the merge candidates instead of failing the whole run — the swarm
        # entry already blocks revise/review on empty projects, this is the
        # second line of defense.
        target, reason = await resolve_chapter_target(context, object_label="改写")
        if target is None:
            return await _draft_new_chapter(context, merge_candidate, reason=reason or "无法确定改写对象")  # 模型调用在事务外
        scene = {"id": target.chapter_id, "title": target.title, "content": target.content}
    if scene is not None and merge_candidate is not None and not (chapter_id and base_version_id):
        # Pipeline rewrite mode (SceneDraft + MergeCandidate upstream, no
        # chapter target). Runs WITH chapter_id/base_version_id keep the
        # approval-bound V2 proposal flow below.
        return await _rewrite_final_draft(context, scene, merge_candidate)  # 模型调用在事务外

    payload = build_merge_payload(reviews)
    input_tokens = output_tokens = used_tokens = 0
    if chapter_id and base_version_id:
        appendix, (input_tokens, output_tokens, used_tokens) = await _compose_appendix(context, payload)  # 模型调用在事务外
        async with uow_factory() as uow:
            info = await create_chief_proposal(uow, run_id, reviews, appendix=appendix)
            await uow.commit()
        payload["proposal_id"] = info["proposal_id"]
        payload["guard_status"] = info["guard_status"]
    return RoleResult(
        artifact_type="candidate",
        payload=payload,
        used_tokens=used_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        extra_events=[{"event": "merge.committed", "run_id": run_id, "review_count": len(reviews), **{key: len(payload[key]) for key in _MERGE_BUCKETS}}],
    )
