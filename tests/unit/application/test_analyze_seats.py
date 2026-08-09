"""分析三席位（analyze_structure/cast/hooks -> analyze_merge）纯函数单元测试。

覆盖：
- is_analyze_task_keys 图签名判定（现行四任务 + 存量单任务兼容）；
- _analyze_merge_fallback 确定性兜底：结构骨架 + 伏笔席位按章补齐 hooks，
  产出保持 normalize_chapters 契约（chapter_no/title/summary/hooks/target_words），
  volumes 可选字段在此汇合；
- _hooks_by_chapter 的容错（不可解析 chapter_no 丢弃）；
- 合议归一函数（findings/rulings/instructions）的过滤与严重度排序。
"""

from __future__ import annotations

from proseforge.application.agents.batch_dispatch import (
    normalize_chapters,
    normalize_volumes,
)
from proseforge.application.agents.intent import is_analyze_task_keys
from proseforge.application.agents.review_handlers import (
    _analyze_merge_fallback,
    _hooks_by_chapter,
    _normalize_council_findings,
    _normalize_council_instructions,
    _normalize_council_rulings,
)


def test_is_analyze_task_keys_accepts_both_signatures():
    assert is_analyze_task_keys(["analyze"])  # 存量单任务图
    assert is_analyze_task_keys(["analyze_merge", "analyze_hooks", "analyze_cast", "analyze_structure"])  # 顺序无关
    assert not is_analyze_task_keys(["analyze_structure", "analyze_cast"])  # 缺席位/缺融合
    assert not is_analyze_task_keys(["scene_a", "merge"])
    assert not is_analyze_task_keys([])


_STRUCTURE = {
    "title": "烛龙传",
    "total_chapters": 2,
    "chapters": [
        {"chapter_no": 1, "title": "风起", "summary": "相遇", "target_words": "2500-3500"},
        {"chapter_no": 2, "title": "云涌", "summary": "冲突", "target_words": 3000},
    ],
    "volumes": [{"volume_no": 1, "title": "初起", "chapter_range": "1-2"}],
}
_HOOKS = {
    "chapter_hooks": [
        {"chapter_no": 1, "hooks": "埋入玉佩来历"},
        {"chapter_no": "2", "hooks": "回收玉佩来历"},  # 字符串章号可解析
        {"chapter_no": "x", "hooks": "不可解析丢弃"},
    ]
}
_CAST = {"characters": [{"name": "沈砚", "role": "主角", "arc": "觉醒", "relations": ["唐临川：师徒"]}]}


def test_analyze_merge_fallback_merges_seats_and_keeps_contract():
    payload = _analyze_merge_fallback({
        "analyze_structure": _STRUCTURE,
        "analyze_hooks": _HOOKS,
        "analyze_cast": _CAST,
    })
    assert payload is not None
    chapters = normalize_chapters(payload)
    assert [chapter["chapter_no"] for chapter in chapters] == [1, 2]
    # 伏笔席位按章补齐空 hooks（含字符串章号）
    assert chapters[0]["hooks"] == "埋入玉佩来历"
    assert chapters[1]["hooks"] == "回收玉佩来历"
    # 契约字段原样携带（target_words 区间字符串与整数两形态）
    assert chapters[0]["target_words"] == "2500-3500"
    assert chapters[1]["target_words"] == 3000
    # volumes 在此汇合 + cast 附带
    assert normalize_volumes(payload) == [{"volume_no": 1, "title": "初起", "start": 1, "end": 2}]
    assert payload["cast"] == _CAST["characters"]
    assert payload["title"] == "烛龙传"
    assert payload["total_chapters"] == 2


def test_analyze_merge_fallback_keeps_structure_hooks():
    structure = {"chapters": [{"chapter_no": 1, "title": "风起", "summary": "相遇", "hooks": "结构席位自带"}]}
    payload = _analyze_merge_fallback({"analyze_structure": structure, "analyze_hooks": _HOOKS})
    assert payload is not None
    assert payload["chapters"][0]["hooks"] == "结构席位自带"  # 已有 hooks 不被覆盖


def test_analyze_merge_fallback_without_structure_returns_none():
    assert _analyze_merge_fallback({"analyze_hooks": _HOOKS}) is None
    assert _analyze_merge_fallback({}) is None


def test_hooks_by_chapter_drops_unparsable():
    hooks = _hooks_by_chapter(_HOOKS)
    assert hooks == {1: "埋入玉佩来历", 2: "回收玉佩来历"}


# ---------------------------------------------------------------------------
# 合议归一函数
# ---------------------------------------------------------------------------


def test_normalize_council_findings_dedupes_shapes():
    findings = _normalize_council_findings([
        "纯字符串问题",
        {"finding": "带引文", "severity": "high", "source": "continuity_reviewer", "evidence": ["引文甲"]},
        {"finding": "span 形态", "evidence_spans": [{"quote": "引文乙"}]},
        {"severity": "low"},  # 无 finding 丢弃
        42,
    ])
    assert [item["finding"] for item in findings] == ["纯字符串问题", "带引文", "span 形态"]
    assert findings[1]["source"] == ["continuity_reviewer"]
    assert findings[1]["evidence_spans"] == [{"quote": "引文甲"}]  # legacy 提取器兼容形态
    assert findings[2]["evidence"] == ["引文乙"]


def test_normalize_council_rulings_requires_group_and_winner():
    rulings = _normalize_council_rulings([
        {"conflict_group": "cg-abc", "winner_role": "continuity_reviewer", "resolution": "采纳", "reason": "依据"},
        {"conflict_group": "cg-def"},  # 缺胜方丢弃
        {"winner_role": "style_editor"},  # 缺组丢弃
    ])
    assert rulings == [{"conflict_group": "cg-abc", "winner_role": "continuity_reviewer", "resolution": "采纳", "reason": "依据"}]


def test_normalize_council_instructions_sorted_by_severity():
    instructions = _normalize_council_instructions([
        {"finding": "低危", "severity": "low", "instruction": "改低危", "evidence": ["q1"]},
        {"finding": "高危", "severity": "high", "instruction": "改高危", "evidence": ["q2"]},
        {"finding": "中危"},  # 缺 instruction 时回退 finding
    ])
    assert [item["severity"] for item in instructions] == ["high", "medium", "low"]
    assert instructions[0]["instruction"] == "改高危"
    assert instructions[1]["instruction"] == "中危"
