"""Swarm chat intent routing (rule-based, no model call).

classify_intent() decides what a swarm-mode message becomes: a plain
streaming chat reply ("chat") or an agent run from one of the graph
templates in graph_for_intent() ("write" | "review" | "revise" | "analyze").

Precedence: a dumped outline (3+ chapter marks, "第 X 章" or markdown
"# 十七、" headings) short-circuits to analyze ahead of everything — revise
keywords inside a long outline are outline content, not instructions.
Without enough chapter marks: review > anchored-write > revise > write > chat.
The anchored-write tier exists because a single-chapter write request often
quotes its outline inline ("写第3章……本章大纲：……现场改写符文程序……"):
stray revise keywords inside that quoted outline are content, not commands,
so a write instruction anchored at a line start wins over them. Bare revise
instructions ("改写第三章", "重写第5章") are excluded from the anchor by a
negative lookbehind and still classify as revise. All template roles are
registered V3 roles (see
application/agents/role_handlers.py); token_budget stays at the
GraphTaskRequest default (1 = unset) so run budgeting behaves exactly like
manually submitted graphs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from proseforge.application.agents.prompts import persona_for_role

Intent = Literal["write", "review", "revise", "analyze", "chat"]

# Review outranks the others: "审校并改写第三章" is a review request first.
_REVIEW_PATTERN = re.compile(r"审校|审查|评审|检查.{0,6}(章节|正文|稿|内容)")
_REVISE_PATTERN = re.compile(r"改写|重写|润色|修改")
_WRITE_PATTERN = re.compile(r"开始写作|写第|续写|写一章|写几章|帮我写|起草|创作|大纲")
# A write instruction anchored near a line start ("写第3章…", "新写第3章…",
# "续写…") outranks stray revise keywords inside an attached outline. The
# negative lookbehind keeps genuine revise instructions ("改写第三章" /
# "重写第5章") on the revise path.
_ANCHORED_WRITE_PATTERN = re.compile(
    r"(?m)^[^\n]{0,20}?(新写|从零.{0,4}写|(?<![改重润修])写\s*第\s*[0-9一二三四五六七八九十百零]+\s*章|续写)"
)
# A dumped multi-chapter outline: 3+ chapter marks (digits or Chinese
# numerals), counting both "第 X 章" inline marks and markdown chapter
# headings ("# 十七、" / "## 第3章" / "### 第十二章.").
_CHAPTER_MARK_PATTERN = re.compile(r"第\s*[0-9一二三四五六七八九十百零]+\s*章")
_MD_CHAPTER_HEAD_PATTERN = re.compile(r"(?m)^#{1,3}\s*第?\s*[0-9一二三四五六七八九十百零]+\s*[章、.．]")
_ANALYZE_MIN_CHAPTER_MARKS = 3


def _chapter_mark_count(text: str) -> int:
    """Merged chapter-mark count: inline "第 X 章" plus markdown headings."""
    return len(_CHAPTER_MARK_PATTERN.findall(text)) + len(_MD_CHAPTER_HEAD_PATTERN.findall(text))


def classify_intent(text: str) -> Intent:
    if _chapter_mark_count(text) >= _ANALYZE_MIN_CHAPTER_MARKS:
        # A dumped outline outranks revise/review: "修改/改写" inside a long
        # outline describes the chapters, it is not a rewrite instruction.
        return "analyze"
    if _REVIEW_PATTERN.search(text):
        return "review"
    if _ANCHORED_WRITE_PATTERN.search(text):
        # "写第3章……本章大纲：……改写符文……" — the anchored write request
        # wins over revise keywords inside its quoted outline.
        return "write"
    if _REVISE_PATTERN.search(text):
        return "revise"
    if _WRITE_PATTERN.search(text):
        return "write"
    return "chat"


# ---------------------------------------------------------------------------
# Orchestrator (LLM) second-pass classification.
# Used only when the rule classifier says "chat": the orchestrator model
# re-judges the message. One English word out, nothing else.
# ---------------------------------------------------------------------------

_ORCHESTRATOR_FALLBACK_IDENTITY = "你是小说写作助手的总调度。"

_ORCHESTRATOR_CLASSIFICATION_INSTRUCTIONS = """把用户消息分类为下面五类之一，只输出一个英文单词，不要输出任何其他内容：
- write：要求创作新内容（写作、续写、起草、出大纲等）
- review：要求审校、评审、检查已有内容的问题
- revise：要求改写、重写、润色、修改已有内容
- analyze：用户投入完整小说大纲（多章结构），要求解析/拆解/准备写作
- chat：闲聊、问答、与创作执行无关的一切

示例：
用户：帮我把林风的故事继续往下写 -> write
用户：这段有什么问题 -> review
用户：语句再优美点 -> revise
用户：这是我的全书大纲（第一章……第二章……第三章……），帮我拆解成写作计划 -> analyze
用户：今天天气如何 -> chat"""


def orchestrator_intent_prompt() -> str:
    """总调度分类提示词：orchestrator 人格文件主体 + 分类指令；orchestrator.md 缺失回退 chief_planner.md，再缺失回退内置身份行。

    Built per call (persona_for_role rereads the persona file on mtime
    change) so editing packs/personas/orchestrator.md takes effect
    without a restart; call frequency is low enough that a per-call stat
    is not a performance concern.
    """
    persona = persona_for_role("orchestrator") or persona_for_role("chief_planner") or _ORCHESTRATOR_FALLBACK_IDENTITY
    return f"{persona}\n{_ORCHESTRATOR_CLASSIFICATION_INSTRUCTIONS}"

_INTENT_ANSWER_PATTERN = re.compile(r"\b(write|review|revise|analyze|chat)\b", re.IGNORECASE)


def parse_intent_answer(text: str) -> Intent | None:
    """Extract the intent word from an orchestrator model reply.

    Case-insensitive and punctuation-tolerant ("Write.", "答案是 review");
    returns None when none of the five words is present.
    """
    match = _INTENT_ANSWER_PATTERN.search(text or "")
    return match.group(1).lower() if match else None  # type: ignore[return-value]


# analyze 意图四任务图签名（三专项席位并行 + 融合）；存量单任务图（["analyze"]）
# 在批量调度/写作状态/运行总结三处同样被认作 analyze run（向后兼容）。
ANALYZE_TASK_KEYS: frozenset[str] = frozenset({"analyze_structure", "analyze_cast", "analyze_hooks", "analyze_merge"})


def is_analyze_task_keys(task_keys: Iterable[str]) -> bool:
    """analyze 意图图签名判定：现行四任务图与存量单任务图都认。"""
    keys = frozenset(str(key) for key in task_keys)
    return keys == frozenset({"analyze"}) or keys == ANALYZE_TASK_KEYS


def graph_for_intent(intent: Intent) -> list[dict[str, object]]:
    """GraphTaskRequest-shaped dicts ({id, role, depends_on}) for an intent.

    write is the full 17-task pipeline (plan -> characters -> promise
    contract -> 4 parallel scene drafts (scene_d is the 人味写作 seat) ->
    collaborative fusion of the drafts (select) -> 3 parallel
    reviews -> review council (合议裁定冲突) -> merge -> rewrite -> recheck
    -> promise verify -> promise register); review is the 3-reviewer
    parallel battery; revise is merge -> rewrite -> recheck (merge tolerates
    an empty review set and produces empty-bucket candidates); analyze is
    the three-seat battery (structure/cast/hooks analysts in parallel) ->
    analyze_merge fusion into the per-chapter workflow. Raises ValueError
    for "chat" — chat never creates a run.

    Standalone review/revise runs have no upstream artifacts: the handlers
    (review_handlers._run_reviewer, chief_handler.chief_editor_handler)
    inject the chapter full text resolved from run.chapter_id or the goal's
    chapter number (see review_target.resolve_chapter_target), and fail the
    task with a Chinese-readable reason when no target can be determined —
    they never spin an empty pipeline.
    """
    if intent == "analyze":
        # 分析三席位（福尔摩斯协作化）：结构/人物/伏笔三个专项 analyst 并行
        # 互不可见，analyze_merge（merge_editor 角色）融合成最终逐章工作流。
        return [
            {"id": "analyze_structure", "role": "analyst", "depends_on": []},
            {"id": "analyze_cast", "role": "analyst", "depends_on": []},
            {"id": "analyze_hooks", "role": "analyst", "depends_on": []},
            {"id": "analyze_merge", "role": "merge_editor", "depends_on": ["analyze_structure", "analyze_cast", "analyze_hooks"]},
        ]
    if intent == "write":
        return [
            {"id": "planner", "role": "chief_planner", "depends_on": []},
            {"id": "character", "role": "character_designer", "depends_on": ["planner"]},
            # promise_keeper（奥莉维亚）三节点：契约卡在场景草稿前产出并注入
            # scene_writer；核对在终审后逐条判兑现；登记在图尾提取新承诺。
            # task_key 一律 promise_ 前缀，避开门禁的硬编码名单
            # （workflows/agent_executor.py 的 review_*/merge/rewrite/recheck）。
            {"id": "promise_contract", "role": "promise_keeper", "depends_on": ["character"]},
            {"id": "scene_a", "role": "scene_writer", "depends_on": ["character", "promise_contract"]},
            {"id": "scene_b", "role": "scene_writer", "depends_on": ["character", "promise_contract"]},
            {"id": "scene_c", "role": "scene_writer", "depends_on": ["character", "promise_contract"]},
            # 第四路草稿：人味写作席位（去 AI 味专攻）。人格走
            # packs/personas/scene_d.md，goal_hint 追加 HUMAN_FLAVOR_GUIDE。
            {"id": "scene_d", "role": "scene_writer", "depends_on": ["character", "promise_contract"]},
            {"id": "select", "role": "merge_editor", "depends_on": ["scene_a", "scene_b", "scene_c", "scene_d"]},
            {"id": "review_continuity", "role": "continuity_reviewer", "depends_on": ["select"]},
            {"id": "review_adversarial", "role": "adversarial_reviewer", "depends_on": ["select"]},
            {"id": "review_style", "role": "style_editor", "depends_on": ["select"]},
            # 评审合议（约翰逊协作化）：三评审互不可见，合议主持（merge_editor
            # 角色，真实调模型）去重 findings、裁定 wire_conflicts 冲突组、
            # 产出按严重度排序的改写指令清单；门禁 PASS 时随改写链一并 SKIPPED。
            {"id": "review_council", "role": "merge_editor", "depends_on": ["review_continuity", "review_adversarial", "review_style"]},
            {"id": "merge", "role": "merge_editor", "depends_on": ["review_council"]},
            {"id": "rewrite", "role": "chief_editor", "depends_on": ["merge"]},
            {"id": "recheck", "role": "continuity_reviewer", "depends_on": ["rewrite"]},
            {"id": "promise_verify", "role": "promise_keeper", "depends_on": ["recheck"]},
            {"id": "promise_register", "role": "promise_keeper", "depends_on": ["promise_verify"]},
        ]
    if intent == "review":
        return [
            {"id": "review_continuity", "role": "continuity_reviewer", "depends_on": []},
            {"id": "review_adversarial", "role": "adversarial_reviewer", "depends_on": []},
            {"id": "review_style", "role": "style_editor", "depends_on": []},
        ]
    if intent == "revise":
        return [
            {"id": "merge", "role": "merge_editor", "depends_on": []},
            {"id": "rewrite", "role": "chief_editor", "depends_on": ["merge"]},
            {"id": "recheck", "role": "continuity_reviewer", "depends_on": ["rewrite"]},
        ]
    raise ValueError(f"no graph template for intent: {intent}")
