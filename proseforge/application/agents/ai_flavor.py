"""AI 味治理三件套（蓝图：反 AI 腔禁令 + 约翰逊确定性 AI 腔检测 + scene_d 人味席位）。

- 写作侧（全员）：``WRITING_STYLE_RULES`` 禁令文本注入 scene_writer 的
  goal_hint（与篇幅硬要求同款注入点，见 role_handlers._run_default_handler）。
- 写作侧（scene_d）：``HUMAN_FLAVOR_GUIDE`` 人味专令只注入 scene_d 的
  goal_hint——第四路并行草稿专攻去 AI 味，由 select 与其余三版择优；
  完整方法论见 packs/personas/references/human-flavor-guide.md。
- 审校侧：``detect_ai_flavor`` 确定性阈值规则（不调模型、不主观打分），
  产出的 issue 由 review_handlers 合并进 continuity_reviewer 报告的
  issues 列表（severity=high + evidence_spans），走 quality_gate 现有
  evidenced-high 计数逻辑自然打回，不新造打分体系。

词表与阈值均为模块常量，后续可配置化；阈值数值与蓝图约定一致：
套话 ≥3 处/千字、段首重复率 >40%、连续 ≥3 段总结式结尾。
"""

from __future__ import annotations

import re
from collections import Counter

# --- 写作侧：反 AI 腔禁令清单（注入 scene_writer goal_hint，控制在 500 字内） ---

WRITING_STYLE_RULES = (
    "【反 AI 腔写作规范】硬性禁令：\n"
    "1. 禁用套话：「他突然意识到」「空气仿佛凝固」「嘴角勾起一抹」「眼中闪过一丝」"
    "「时间仿佛静止」「心跳漏了一拍」「嘴角微微上扬」「深吸一口气」「不由自主地」"
    "「仿佛在诉说着」等 AI 高频句式一律不得出现，用具体动作与细节代替。\n"
    "2. 禁止总结式段落结尾：段末不写「这一刻，他明白了……」式感悟总结，情绪交给场景与动作。\n"
    "3. 禁止排比三连：不连续使用三个结构相同的短句/分句堆砌气势。\n"
    "4. 禁止每段对话后机械接动作描写（「他说……他转身……」式固定节拍），对话可单独成段、可留白。\n"
    "5. 句式长短交错，避免句句等长；用具体感官细节（气味、温度、声响）替代抽象情绪词"
    "（紧张、悲伤、愤怒）。\n"
    "6. 场景可留白：不必每段都交代完整因果，保留未说尽之处。"
)

# --- scene_d（人味写作席位）专项注入：指南蒸馏版，完整版见 ---
# packs/personas/references/human-flavor-guide.md

HUMAN_FLAVOR_GUIDE = (
    "【人味写作专令】你是四路草稿中专攻去 AI 味的一版，以下纪律与篇幅硬要求同级：\n"
    "1. 情绪不出场：全文不直接命名情绪（紧张、悲伤、愤怒、喜悦），用身体反应、物件和异常行为作证据。\n"
    "2. 欲望先行：每个人物在这场戏里有明确的想要或害怕，对话与动作都从此出发。\n"
    "3. 对白必有潜台词：至少一处答非所问、说半句收回或反话客套；禁止人物轮流介绍背景。\n"
    "4. 不体面的第一反应：私心、回避、自我欺骗至少占一个节拍，拒绝全员理性体面。\n"
    "5. 句长交错：长句铺陈后接短句砸实，打散一切排比三连与等长句串。\n"
    "6. 结尾落在实物：场景与章节结尾停在动作、物件或未回答的问题上，禁止感悟升华。\n"
    "7. 细节必须挂钩人物、伏笔或本场主感官，写不出挂钩理由的细节删除。\n"
    "8. 留白合法：反常行为不解释，因果可后补，未说尽之处是读者翻页的动力。"
)

# --- 审校侧：确定性 AI 腔检测 ---

# 中文 AI 高频套话词表。维护说明：线上审校/人工抽查发现新的 AI 腔套话时
# 直接追加到本表；保持短语级（≥4 字、带语境特征）以减少误伤正常表达，
# 泛用单字/双字词（「突然」「仿佛」）不收，由段尾规则兜底。
AI_CLICHE_TERMS: tuple[str, ...] = (
    "他突然意识到",
    "他忽然意识到",
    "她突然意识到",
    "空气仿佛凝固",
    "嘴角勾起一抹",
    "嘴角噙着一抹",
    "嘴角微微上扬",
    "眼中闪过一丝",
    "眼底闪过一抹",
    "时间仿佛静止",
    "心跳漏了一拍",
    "深吸一口气",
    "不由自主地",
    "仿佛在诉说着",
    "不禁想起了",
    "下意识地",
    "空气中弥漫着",
    "一股莫名的",
    "心中涌起一股",
    "前所未有的",
    "久久无法平静",
    "久久不能平静",
    "瞳孔骤然收缩",
    "心脏剧烈地跳动",
    "声音有些颤抖",
    "眉头微微皱起",
    "一抹不易察觉的",
    "如释重负地叹了口气",
    "空气在这一刻凝固",
    "时间在这一刻静止",
)

# 套话命中密度阈值：≥3 处/千字报 issue；同时要求绝对命中数 ≥3，
# 避免短文本（如数百字片段）单次命中即触发的密度放大误报。
CLICHE_HITS_PER_1000_CHARS = 3
CLICHE_MIN_ABSOLUTE_HITS = 3

# 段首重复率阈值：段落开头（前 4 字）的重复占比 >40% 报 issue；
# 少于 5 段的文本不判定（样本太小无统计意义）。
PARA_START_PREFIX_CHARS = 4
PARA_START_REPEAT_RATIO = 0.4
PARA_START_MIN_PARAGRAPHS = 5

# 段尾模式化：段尾（末 20 字）含以下总结/感受句式标记视为「总结式结尾」，
# 连续 ≥3 段即报 issue（与 WRITING_STYLE_RULES 第 2 条禁令对应）。
PARA_ENDING_MARKERS: tuple[str, ...] = ("仿佛", "似乎", "原来", "这一刻", "那一刻")
PARA_ENDING_RUN_MIN = 3
_PARA_ENDING_TAIL_CHARS = 20

# 单条 issue 携带的证据上限：review 行总证据预算 MAX_EVIDENCE_SPANS=32，
# 单条 issue 封顶 8 条，避免一次密集命中吃光评审证据预算。
_ISSUE_EVIDENCE_CAP = 8

_PARAGRAPH_PATTERN = re.compile(r"[^\n]+")


def _paragraphs_with_offsets(text: str) -> list[tuple[int, str]]:
    """正文 → [(起始偏移, 非空段落)]；按换行切段（空行与单换行均分段）。"""
    result: list[tuple[int, str]] = []
    for match in _PARAGRAPH_PATTERN.finditer(text):
        paragraph = match.group(0).strip()
        if paragraph:
            result.append((match.start(), paragraph))
    return result


def _cliche_issue(text: str) -> dict[str, object] | None:
    """规则 1：套话命中密度 ≥ CLICHE_HITS_PER_1000_CHARS 处/千字（且绝对数达标）。"""
    hits: list[tuple[int, str]] = []
    for term in AI_CLICHE_TERMS:
        start = text.find(term)
        while start != -1:
            hits.append((start, term))
            start = text.find(term, start + 1)
    hits.sort()
    if len(hits) < CLICHE_MIN_ABSOLUTE_HITS:
        return None
    density = len(hits) * 1000 / max(len(text), 1)
    if density < CLICHE_HITS_PER_1000_CHARS:
        return None
    terms = sorted({term for _, term in hits})
    return {
        "type": "ai_flavor",
        "rule": "cliche_terms",
        "finding": (
            f"AI 套话密度超标：{len(hits)} 处 / {len(text)} 字"
            f"（阈值 {CLICHE_HITS_PER_1000_CHARS} 处/千字），命中：{'、'.join(terms[:10])}"
        ),
        "evidence": [
            {"start": position, "end": position + len(term), "quote": term}
            for position, term in hits[:_ISSUE_EVIDENCE_CAP]
        ],
    }


def _paragraph_start_issue(paragraphs: list[tuple[int, str]]) -> dict[str, object] | None:
    """规则 2：段首（前 4 字）重复率 >40%（段落数 ≥5 才判定）。"""
    if len(paragraphs) < PARA_START_MIN_PARAGRAPHS:
        return None
    prefixes = [paragraph[:PARA_START_PREFIX_CHARS] for _, paragraph in paragraphs]
    ratio = (len(prefixes) - len(set(prefixes))) / len(prefixes)
    if ratio <= PARA_START_REPEAT_RATIO:
        return None
    common, count = Counter(prefixes).most_common(1)[0]
    evidence = [
        {"start": offset, "end": offset + len(common), "quote": common}
        for (offset, _), prefix in zip(paragraphs, prefixes, strict=True)
        if prefix == common
    ]
    return {
        "type": "ai_flavor",
        "rule": "paragraph_start_repeat",
        "finding": (
            f"段落开头重复率 {ratio:.0%} 超标（阈值 {PARA_START_REPEAT_RATIO:.0%}）："
            f"最常见开头「{common}」出现 {count} 次，AI 式固定起段节拍"
        ),
        "evidence": evidence[:_ISSUE_EVIDENCE_CAP],
    }


def _paragraph_ending_issue(paragraphs: list[tuple[int, str]]) -> dict[str, object] | None:
    """规则 3：连续 ≥3 段以总结式收尾（段尾含 仿佛/似乎/原来/这一刻/那一刻）。"""
    best_run: list[tuple[int, str]] = []
    current_run: list[tuple[int, str]] = []
    for entry in paragraphs:
        tail = entry[1][-_PARA_ENDING_TAIL_CHARS:]
        if any(marker in tail for marker in PARA_ENDING_MARKERS):
            current_run.append(entry)
        else:
            if len(current_run) > len(best_run):
                best_run = current_run
            current_run = []
    if len(current_run) > len(best_run):
        best_run = current_run
    if len(best_run) < PARA_ENDING_RUN_MIN:
        return None
    return {
        "type": "ai_flavor",
        "rule": "paragraph_ending_pattern",
        "finding": (
            f"连续 {len(best_run)} 段以总结/感受句式收尾（段尾含 "
            f"{'/'.join(PARA_ENDING_MARKERS)}），AI 式感悟总结节拍"
        ),
        "evidence": [
            {"start": offset, "end": offset + len(paragraph), "quote": paragraph[-30:]}
            for offset, paragraph in best_run[:_ISSUE_EVIDENCE_CAP]
        ],
    }


def detect_ai_flavor(text: str) -> list[dict[str, object]]:
    """确定性 AI 腔检测：返回 issue 列表（每条含 type/rule/finding/evidence）。

    evidence 条目为 {"start", "end", "quote"}（位置相对输入 text），由审校
    handler 映射为评审 findings 的 evidence_spans；空文本/未命中返回 []。
    """
    if not text or not text.strip():
        return []
    issues: list[dict[str, object]] = []
    cliche = _cliche_issue(text)
    if cliche is not None:
        issues.append(cliche)
    paragraphs = _paragraphs_with_offsets(text)
    start_issue = _paragraph_start_issue(paragraphs)
    if start_issue is not None:
        issues.append(start_issue)
    ending_issue = _paragraph_ending_issue(paragraphs)
    if ending_issue is not None:
        issues.append(ending_issue)
    return issues
