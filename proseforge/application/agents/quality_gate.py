"""Deterministic quality gate for the write pipeline (no model call).

Runs after the three review tasks succeed, before merge is claimed:
- word count: goal may carry a range like "3000-5000字" (lower bound wins,
  default 2500); the scene's content must reach it.
- required clue: goal may carry "必须埋入或回收的线索：X" (up to the first
  。or newline), or a batch-dispatch "伏笔/钩子：X" line (directive words
  filtered out); at least one clue must hit the scene content. A clue hits
  when, after stripping punctuation from the content, either it appears
  verbatim or its longest non-overlapping fragments (>=2 chars) cover
  >= CLUE_COVERAGE_THRESHOLD of it — so "雾江七号，三号货舱" hits
  "雾江七号三号货舱" and separately-located "货轮"/"大副" hits "货轮大副".
- reviews: findings with severity=high AND non-empty evidence_spans fail
  the gate only when their count reaches GATE_EVIDENCED_HIGH_THRESHOLD;
  a single evidenced high is downgraded to a recorded warning (sampling
  showed lone highs rarely hurt book quality), and evidence-less highs
  are ignored entirely — hallucination-prone.
- repetition: CJK n-grams (4~6 chars) appearing over a per-size threshold
  (4-gram >= 8, 5-gram >= 6, 6-gram >= 5 — tuned offline against real
  chapters so legitimate proper-noun frequency stays below the bar) fail
  the gate (function-word runs and single-character repeats excluded;
  longer hits shadow their sub-grams).

PASS -> merge/rewrite/recheck are SKIPPED; otherwise the revise stage
runs. scene_payload=None (scene task failed) always fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WORD_RANGE_PATTERN = re.compile(r"(\d{3,5})\s*[-~—到至]\s*(\d{3,5})\s*字")
# 显式目标行：batch_dispatch.chapter_goal 注入的「目标字数：不少于 X 字」，
# 优先级最高（这是流水线自己的契约，不会被大纲正文污染）。
_EXPLICIT_TARGET_PATTERN = re.compile(r"目标字数[：:]\s*(?:不少于|约|至少)?\s*(\d{3,5})")
# 带限定词的单值（「每章约3000字」「不少于2500字」）；裸「3000字」不匹配，
# 防止大纲正文里的数字（人名代号、年份、数量）被误判成字数目标。
_QUALIFIED_SINGLE_PATTERN = re.compile(r"(?:约|不少于|至少|每章)\s*(\d{3,5})\s*字")
_CLUE_PATTERN = re.compile(r"必须埋入或回收的线索[：:]([^。\n]+)")
# 批量派发 goal 的「伏笔/钩子：X」行（batch_dispatch.chapter_goal）。
_BATCH_HOOKS_PATTERN = re.compile(r"伏笔/钩子[：:]([^\n]+)")
# 「伏笔/钩子」行里的指令词（非线索本体）：前缀先剥离（「回收师父的遗言」→
# 「师父的遗言」），残留含元词汇的段整体丢弃，以免把指令当必含线索。
_HOOK_PREFIXES = ("埋入", "回收", "铺垫", "暗示", "照应")
_HOOK_META_WORDS = ("伏笔", "钩子", "埋入", "回收", "线索", "铺垫", "暗示", "本章", "暂无", "延续", "照应")
_CJK_RUN_PATTERN = re.compile(r"[一-鿿]{2,}")
# 命中判定前对正文归一化：剥掉标点/空白等非文字字符（保留 CJK 与数字），
# 使「雾江七号，三号货舱」与「雾江七号三号货舱」视为同一串。
_NON_WORD_PATTERN = re.compile(r"[^一-鿿0-9]+")
DEFAULT_MIN_WORDS = 2500
# 线索模糊命中阈值：最长不重叠片段（≥2 字）覆盖率 ≥ 0.6 即算命中。
# 精确子串永远命中；阈值只兜住被标点/助词（的、了）打断的写法。
CLUE_COVERAGE_THRESHOLD = 0.6
# 有证据 high 问题的阻断阈值：抽样显示单条 evidenced high 多数不影响成书
# 质量，故 >= 2 条才卡章；1 条降级为警告（记录到 gate.evaluated 事件）。
GATE_EVIDENCED_HIGH_THRESHOLD = 2
# 复读阈值（按 n-gram 长度分档）：长片段更可能是独特短语，阈值放低；
# 短片段容易误伤专名高频（「法医中心」一章出现 6 次属正常），阈值放高。
# 阈值经 64 章真实稿件离线调优（tmp 报告：现状 3 次阈值 100% 误报）。
_REPETITION_THRESHOLDS: dict[int, int] = {6: 5, 5: 6, 4: 8}
# 复读检测的 n-gram 窗口（从长到短处理：长片段命中后其覆盖的短片段不再计）。
_REPETITION_NGRAM_SIZES = (6, 5, 4)
# 常见虚字：片段中虚字占比超过 1/3 视为语气串，不算复读。
_FUNCTION_CHARS = frozenset("的了是在我你他她它们这那有就也都不要会能着过地得好儿没把说看点头来去上里中个么再又还")
_FUNCTION_CHAR_RATIO = 3


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    # Non-blocking observations (e.g. a single evidenced high below the
    # fail threshold); callers record them on the gate.evaluated event.
    warnings: list[str] = field(default_factory=list)


def parse_min_words(goal: str) -> int:
    """goal 里的字数下限：显式目标行 > 区间（取下限）> 限定词单值 > 默认 2500。"""
    text = goal or ""
    match = _EXPLICIT_TARGET_PATTERN.search(text)
    if match:
        return int(match.group(1))
    match = _WORD_RANGE_PATTERN.search(text)
    if match:
        return int(match.group(1))
    match = _QUALIFIED_SINGLE_PATTERN.search(text)
    if match:
        return int(match.group(1))
    return DEFAULT_MIN_WORDS


def parse_required_clues(goal: str) -> list[str]:
    """goal 里的必含线索：显式「必须埋入或回收的线索：X」优先；否则认批量
    goal 的「伏笔/钩子：X」行（过滤指令词，只留线索本体的 CJK 短语）。"""
    text = goal or ""
    match = _CLUE_PATTERN.search(text)
    if match:
        return _CJK_RUN_PATTERN.findall(match.group(1))
    hooks = _BATCH_HOOKS_PATTERN.search(text)
    if not hooks:
        return []
    clues: list[str] = []
    for run in _CJK_RUN_PATTERN.findall(hooks.group(1)):
        for prefix in _HOOK_PREFIXES:
            if run.startswith(prefix) and len(run) > len(prefix) + 1:
                run = run[len(prefix):]
                break
        if len(run) >= 2 and not any(meta in run for meta in _HOOK_META_WORDS):
            clues.append(run)
    return clues


def _clue_hit(clue: str, normalized_text: str) -> bool:
    """线索是否在归一化正文中命中：精确子串优先；否则取线索的最长不重叠
    片段（≥2 字、贪心从长到短），覆盖率 >= CLUE_COVERAGE_THRESHOLD 即命中。
    片段允许分处出现（「货轮」与「大副」各自命中「货轮大副」）。"""
    if clue in normalized_text:
        return True
    if len(clue) < 3:
        return False
    covered = [False] * len(clue)
    for frag_len in range(len(clue) - 1, 1, -1):
        for start in range(len(clue) - frag_len + 1):
            if any(covered[start : start + frag_len]):
                continue
            if clue[start : start + frag_len] in normalized_text:
                for index in range(start, start + frag_len):
                    covered[index] = True
    return sum(covered) / len(clue) >= CLUE_COVERAGE_THRESHOLD


def _repetition_findings(text: str) -> list[str]:
    """确定性复读检测：归一化正文上做 CJK n-gram（4~6 字）滑动计数，
    同一 4/5/6 字片段分别出现 >= 8/6/5 次记为复读。虚字占比超 1/3 的
    语气串与单字重复（「字字字字」）不计；从长到短处理，长片段命中后
    其覆盖的短片段不再重复上报。只报计数最高的前 3 个。"""
    normalized = _NON_WORD_PATTERN.sub("", text)
    min_size = min(_REPETITION_NGRAM_SIZES)
    if len(normalized) < min_size * _REPETITION_THRESHOLDS[min_size]:
        return []
    accepted: list[tuple[str, int]] = []
    for size in _REPETITION_NGRAM_SIZES:
        threshold = _REPETITION_THRESHOLDS[size]
        counts: dict[str, int] = {}
        for start in range(len(normalized) - size + 1):
            gram = normalized[start : start + size]
            if len(set(gram)) == 1:
                continue
            if sum(1 for char in gram if char in _FUNCTION_CHARS) * _FUNCTION_CHAR_RATIO > size:
                continue
            counts[gram] = counts.get(gram, 0) + 1
        for gram, count in counts.items():
            if count < threshold:
                continue
            if any(gram in longer for longer, _longer_count in accepted):
                continue
            accepted.append((gram, count))
    top = sorted(accepted, key=lambda item: item[1], reverse=True)[:3]
    if not top:
        return []
    return ["复读超标：" + "、".join(f"「{gram}」出现 {count} 次" for gram, count in top)]


def _findings_with_evidence(review_payloads: list[dict]) -> int:
    count = 0
    for payload in review_payloads:
        if not isinstance(payload, dict):
            continue
        for key in ("findings", "issues", "risks"):
            findings = payload.get(key)
            if not isinstance(findings, list):
                continue
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                if str(finding.get("severity", "")).lower() != "high":
                    continue
                if finding.get("evidence_spans"):
                    count += 1
    return count


def evaluate_gate(*, goal: str, scene_payload: dict | None, review_payloads: list[dict]) -> GateResult:
    if scene_payload is None:
        return GateResult(passed=False, reasons=["scene missing"])
    content = scene_payload.get("content")
    text = content if isinstance(content, str) else ""
    reasons: list[str] = []
    warnings: list[str] = []

    min_words = parse_min_words(goal)
    if len(text) < min_words:
        reasons.append(f"字数不足：{len(text)} < {min_words}")

    reasons.extend(_repetition_findings(text))

    clues = parse_required_clues(goal)
    if clues:
        normalized = _NON_WORD_PATTERN.sub("", text)
        missed = [clue for clue in clues if not _clue_hit(clue, normalized)]
        if len(missed) == len(clues):
            reasons.append(f"必含线索未命中：{'、'.join(missed[:5])}")

    evidenced_highs = _findings_with_evidence(review_payloads)
    if evidenced_highs >= GATE_EVIDENCED_HIGH_THRESHOLD:
        reasons.append(f"评审发现 {evidenced_highs} 条有证据的 high 问题")
    elif evidenced_highs:
        # Lone evidenced high: downgrade to a recorded warning, never block.
        warnings.append(f"评审发现 {evidenced_highs} 条有证据的 high 问题（低于阻断阈值 {GATE_EVIDENCED_HIGH_THRESHOLD}，放行并记录）")

    return GateResult(passed=not reasons, reasons=reasons, warnings=warnings)
