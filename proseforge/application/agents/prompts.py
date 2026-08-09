"""V3 角色提示词模板（蓝图 V3-004/005）。

每个角色的系统提示词都固定要求“只输出一个 JSON 对象”，与
role_handlers 的默认 handler（prompt → 解析 JSON → 校验 → Artifact）配套。
提示词只约束内容形态，不要求模型自报 artifact_type——类型由服务端按
RolePolicy allowlist 决定（domain/agents/roles.py，policy workstream 所有）。
"""

from __future__ import annotations

from pathlib import Path

JSON_OUTPUT_INSTRUCTION = "只输出一个 JSON 对象，不要输出 Markdown 代码围栏或任何额外解释。"

ROLE_PROMPTS: dict[str, str] = {
    "chief_planner": "你是主编策划 Agent。把写作目标拆解为结构化大纲候选，给出各章节的标题与要点规划。",
    "story_architect": "你是故事架构 Agent。设计主线结构、关键转折点与冲突升级路径。",
    "world_builder": "你是世界观构建 Agent。产出结构化世界观规则候选，每条规则注明适用范围与约束。",
    "character_designer": "你是角色设计 Agent。产出角色卡：姓名、故事定位、性格特质与动机。",
    "timeline_analyst": "你是时间线分析 Agent。梳理事件时间线，报告时序冲突与漏洞。",
    "scene_writer": "你是场景写作 Agent。根据上游 Artifact 写出一个完整场景草稿，保持设定连续。",
    "style_editor": "你是文风编辑 Agent。审查文字风格一致性，给出问题清单与修改方向。",
    "continuity_reviewer": "你是连续性评审 Agent。核对设定、角色与剧情的连续性，逐条报告发现。",
    "adversarial_reviewer": "你是对抗性评审 Agent。主动寻找逻辑漏洞、设定矛盾与风险点。",
    "merge_editor": "你是合并编辑 Agent。把多份候选合并为一份自洽的合并候选，保留冲突双方的依据。",
    "chief_editor": "你是主编 Agent。综合全部上游 Artifact 与评审结论，产出最终修订建议。",
    "analyst": "你是分析 Agent，总调度的秘书。把用户投入的小说大纲解析成结构化的逐章工作流：每章给出章节号、标题、剧情摘要、必须埋入或回收的线索、建议字数区间。保持大纲原意，不创作正文。",
    "promise_keeper": "你是动态承诺档案官 Agent（奥莉维亚）。只依据承诺台账与证据引文判断伏笔/钩子的埋设与兑现，不创作剧情、不擅自改设定。",
}

DEFAULT_PROMPT = "你是 ProseForge 的写作 Agent。完成分配给你的任务，输出结构化结果。"

# --- 人格文件加载（packs/personas/<role>.md，mtime 缓存，缺失回退 ROLE_PROMPTS） ---

DEFAULT_PERSONAS_DIR = "packs/personas"

# persona 文件路径 → (文件 mtime, 内容)；mtime 变化即重新读取
_persona_cache: dict[str, tuple[float, str]] = {}


def _personas_dir() -> str:
    """人格目录：从 settings 读（与 skills_dir 同款机制），惰性 import 避免循环依赖。"""
    from proseforge.settings import get_settings

    return get_settings().personas_dir


def persona_for_role(role: str, personas_dir: str | None = None) -> str | None:
    """加载角色人格文件内容；文件缺失/损坏/为空返回 None（回退 ROLE_PROMPTS）。"""
    path = Path(personas_dir or _personas_dir()) / f"{role}.md"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cache_key = str(path)
    cached = _persona_cache.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None  # 单个人格文件损坏不拖垮整个提示词链路
    if not text:
        return None
    _persona_cache[cache_key] = (mtime, text)
    return text


# 各角色输出 JSON 的建议形态（内容约束，非服务端 schema；校验以 role_handlers 为准）
ROLE_OUTPUT_HINTS: dict[str, str] = {
    "chief_planner": '形如 {"title": "...", "chapters": [{"title": "...", "summary": "..."}], "scene_bridge": [{"time_anchor": "...", "space_anchor": "...", "emotion_anchor": "...", "pov_anchor": "...", "purpose": "...", "ending_hook": "..."}]}，scene_bridge 为 3 个场景的衔接规划（time_anchor=时间锚：紧接上一场景何时/隔多久，space_anchor=空间锚：人物如何到达，emotion_anchor=情绪锚：承接上场景的哪种情绪余波，pov_anchor=视角锚：本章锁定视角人物，purpose=本场景任务，ending_hook=给下一场景的接力句），可选但应当输出',
    "story_architect": '形如 {"title": "...", "chapters": [{"title": "...", "summary": "..."}]}',
    "world_builder": '形如 {"rule": "...", "scope": "..."}',
    "character_designer": '形如 {"name": "...", "role": "...", "traits": ["..."], "voice": {"dialect": "...", "catchphrases": ["..."], "register": "...", "sentence_len": "...", "connectors": ["..."], "banned_words": ["..."], "emotion_baseline": "..."}, "emotion": "...", "mental": "..."}，主要角色必须带 voice 声纹档案（dialect 无方言填"普通话"，catchphrases 适度），emotion/mental 为精神状态基线',
    "timeline_analyst": '形如 {"events": ["..."], "issues": ["..."]}',
    "scene_writer": '形如 {"title": "...", "content": "..."}',
    "style_editor": '形如 {"summary": "...", "findings": [{"finding": "...", "severity": "low|medium|high", "target_artifact_id": "...", "evidence_spans": [{"artifact_id": "...", "start": 0, "end": 1, "quote": "..."}], "verdict": "PASS|WARNING|CONFLICT|UNSUPPORTED"}]}，证据区间必须引用上游 artifact_id，无证据时 verdict=UNSUPPORTED 且 evidence_spans 为空',
    "continuity_reviewer": '形如 {"summary": "...", "findings": [{"finding": "...", "severity": "low|medium|high", "target_artifact_id": "...", "evidence_spans": [{"artifact_id": "...", "start": 0, "end": 1, "quote": "..."}], "verdict": "PASS|WARNING|CONFLICT|UNSUPPORTED"}]}，证据区间必须引用上游 artifact_id，无证据时 verdict=UNSUPPORTED 且 evidence_spans 为空',
    "adversarial_reviewer": '形如 {"summary": "...", "findings": [{"finding": "...", "severity": "low|medium|high", "target_artifact_id": "...", "evidence_spans": [{"artifact_id": "...", "start": 0, "end": 1, "quote": "..."}], "verdict": "PASS|WARNING|CONFLICT|UNSUPPORTED"}]}，证据区间必须引用上游 artifact_id，无证据时 verdict=UNSUPPORTED 且 evidence_spans 为空',
    "merge_editor": '形如 {"summary": "...", "agreements": ["..."], "conflicts": [{"conflict_group": "...", "parties": ["..."], "claims": ["..."], "resolution": null}], "unsupported": ["..."], "accepted": ["..."]}，只做分类，不改写作者正文',
    "chief_editor": '形如 {"summary": "...", "appendix": "..."}，appendix 是追加在正文后的合并附录（落实一致与已接受发现），不得改写原文',
    # analyst chapters JSON keys map onto the canonical ChapterCard contract
    # (domain/dispatch/task_plan.py): chapter_no->chapter, summary->summary,
    # hooks->hooks_out. Keys stay as-is for run_summary._analyze_lines compat.
    "analyst": '形如 {"title": "...", "total_chapters": 30, "chapters": [{"chapter_no": 1, "title": "...", "summary": "...", "hooks": "...", "target_words": "2500-3500"}], "volumes": [{"volume_no": 1, "title": "...", "chapter_range": "1-10"}]}，chapters 必须覆盖大纲中的每一章；volumes 为卷结构（卷序号/卷名/章节区间），大纲有分卷时必填，无分卷可省略',
    # promise_keeper 三 task_key 各有契约（promise_handlers 在任务提示词里
    # 追加对应输出指令）；此处给默认 handler 兜底的总形态。
    "promise_keeper": '按 task_key 输出：promise_contract 形如 {"due": [{"key": "...", "source_chapter": 1, "evidence": "...", "required_fulfillments": 1, "remaining": 1, "reason": "..."}], "plant": [{"hook": "...", "note": "..."}], "watch": [{"topic": "...", "note": "..."}]}；promise_verify 形如 {"verdicts": [{"key": "...", "fulfilled": true, "quote": "..."}]}；promise_register 形如 {"promises": [{"key": "...", "category": "伏笔|钩子|承诺|受伤|奖励", "note": "...", "duplicate_of": null}]}',
}


def prompt_for_role(role: str) -> str:
    """角色系统提示词：人格主体 + 输出形态 + JSON-only 指令。

    人格主体优先取 packs/personas/<role>.md 文件内容，文件缺失时回退
    内置 ROLE_PROMPTS；ROLE_OUTPUT_HINTS 拼接逻辑不变。
    """
    base = persona_for_role(role) or ROLE_PROMPTS.get(role, DEFAULT_PROMPT)
    hint = ROLE_OUTPUT_HINTS.get(role)
    if hint:
        return f"{base}\n输出 {hint}。{JSON_OUTPUT_INSTRUCTION}"
    return f"{base}\n{JSON_OUTPUT_INSTRUCTION}"


# task_key 级输出形态：少数任务的 JSON 契约与角色默认形态不同（select 复用
# merge_editor 角色，但产出的是融合定稿而非四桶分类）。优先于 ROLE_OUTPUT_HINTS。
TASK_OUTPUT_HINTS: dict[str, str] = {
    "select": '形如 {"title": "...", "content": "...", "rationale": "...", "backbone": "骨架稿 artifact_id", "sources": ["实际采用的 artifact_id", ...]}',
    # 评审合议（review_council）：去重 findings + 冲突裁定 + 排序改写指令；
    # 每条指令带引文 evidence，供整章改写与定点改写消费。
    "review_council": '形如 {"summary": "...", "findings": [{"finding": "...", "severity": "low|medium|high", "source": ["上报角色", ...], "evidence": ["原文引文", ...]}], "rulings": [{"conflict_group": "...", "winner_role": "裁定成立的一方角色", "resolution": "裁定结论", "reason": "依据"}], "rewrite_instructions": [{"finding": "...", "severity": "low|medium|high", "instruction": "改哪里、为什么改", "evidence": ["原文引文", ...]}]}，rewrite_instructions 按严重度排序（high 在前），每个冲突组都必须有裁定',
    # 分析三席位：各自只关注本职维度；analyze_merge 融合成 analyst 兼容契约。
    "analyze_structure": '形如 {"title": "...", "total_chapters": 30, "chapters": [{"chapter_no": 1, "title": "...", "summary": "...", "target_words": "2500-3500"}], "volumes": [{"volume_no": 1, "title": "...", "chapter_range": "1-10"}], "pacing_notes": ["..."]}，chapters 必须覆盖大纲中的每一章；hooks 留空由伏笔席位补齐；无分卷可省略 volumes',
    "analyze_cast": '形如 {"characters": [{"name": "...", "role": "...", "arc": "...", "relations": ["..."]}], "chapter_cast": [{"chapter_no": 1, "appearing": ["..."], "development": "..."}]}，覆盖大纲里每个有名角色',
    "analyze_hooks": '形如 {"chapter_hooks": [{"chapter_no": 1, "hooks": "埋入/回收的线索"}], "promises": [{"hook": "...", "planted_at": 1, "resolved_at": 10, "note": "..."}], "dangling": ["埋了没收的钩子"]}，每章的 hooks 写清"埋入"还是"回收"',
    "analyze_merge": '形如 {"title": "...", "total_chapters": 30, "chapters": [{"chapter_no": 1, "title": "...", "summary": "...", "hooks": "...", "target_words": "2500-3500"}], "volumes": [{"volume_no": 1, "title": "...", "chapter_range": "1-10"}]}，chapters 必须覆盖大纲中的每一章：结构与字数取自结构席位、hooks 取自伏笔席位、人物弧光变化融入 summary；volumes 为卷结构，大纲有分卷时必填',
}


def prompt_for_task(role: str, task_key: str = "") -> str:
    """任务级系统提示词：task_key 专属人格（packs/personas/<task_key>.md）
    优先于角色人格——scene_d（人味写作席位）靠它与 scene_a/b/c 拉开差异；
    输出形态默认按角色取，TASK_OUTPUT_HINTS 登记的任务按任务取（select 的
    融合定稿形态不同于 merge_editor 的四桶分类形态）。"""
    base = (persona_for_role(task_key) if task_key else None) or persona_for_role(role) or ROLE_PROMPTS.get(role, DEFAULT_PROMPT)
    hint = TASK_OUTPUT_HINTS.get(task_key) or ROLE_OUTPUT_HINTS.get(role)
    if hint:
        return f"{base}\n输出 {hint}。{JSON_OUTPUT_INSTRUCTION}"
    return f"{base}\n{JSON_OUTPUT_INSTRUCTION}"


GOAL_HINT_MAX_CHARS = 4000  # 目标原文注入上限，防止超长 goal 撑爆 prompt


def goal_hint_for(run: dict) -> str:
    """写作目标注入：优先 run.goal 原文（0027 起落库，截断防爆 prompt）；
    存量 run 无 goal 时回退 goal_hash 前缀（不编内容）。"""
    goal = str(run.get("goal") or "").strip()
    if goal:
        return goal[:GOAL_HINT_MAX_CHARS]
    return f"goal_hash:{str(run.get('goal_hash', ''))[:12]}"


def render_memory_lines(memory_slice: list[dict[str, object]] | None) -> list[str]:
    """已批准记忆切片 → 提示词行（与 build_task_prompt 的记忆段同款格式）。

    供不走 build_task_prompt 的专家 handler（评审簇/改写/融合/奥莉维亚）
    统一注入；空切片返回空列表，调用方跳过注入。
    """
    if not memory_slice:
        return []
    lines = ["已批准记忆切片（仅作背景约束，不得复述；token 预算 400 以内）："]
    lines.extend(f"- {item.get('fact_key', '')}: {item.get('value', '')}" for item in memory_slice)
    return lines


def build_task_prompt(*, role: str, task_key: str, goal_hint: str, artifacts: list[dict[str, object]], memory_slice: list[dict[str, object]] | None = None, scene_pack: str | None = None) -> str:
    """用户提示词：任务标识 + 目标摘要 + 上游 Artifact 摘要（preview 已脱敏限长）。

    ``memory_slice`` 为用户已批准的记忆事实（每形 {"fact_key", "value"}，
    条数/长度已由 memory_service 限界）；注入时带显式 token 预算说明。
    ``scene_pack`` 为叙事检索场景包文本（executor 在 work 项目且 RAG
    开关开启时注入），位于记忆切片之后、上游 Artifact 之前。
    """
    lines = [f"任务：{task_key}（角色 {role}）", f"写作目标摘要：{goal_hint}"]
    if memory_slice:
        lines.append("已批准记忆切片（仅作背景约束，不得复述；token 预算 400 以内）：")
        lines.extend(f"- {item.get('fact_key', '')}: {item.get('value', '')}" for item in memory_slice)
    if scene_pack:
        lines.append("叙事检索场景包：")
        lines.append(scene_pack)
    if artifacts:
        lines.append("上游 Artifact 摘要：")
        lines.extend(f"- [{item.get('artifact_type', '')}] {item.get('task_key', '')}: {item.get('preview', '')}" for item in artifacts)
    lines.append(JSON_OUTPUT_INSTRUCTION)
    return "\n".join(lines)
