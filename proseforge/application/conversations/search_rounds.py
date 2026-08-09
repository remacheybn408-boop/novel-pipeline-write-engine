"""Web-search support for chat generation (``builtin-web-search``).

What lives here after tool-system phase 1:
- the proactive intent-driven auto search (``run_auto_search`` — unchanged);
- the user-switch helper (``web_search_switch_enabled``);
- the ``WEB_SEARCH_SKILL_KEY`` constant (single source, imported by the tool
  registry).

The ```search: fence -> search-and-continue rounds moved to the unified tool
protocol: application/conversations/tool_contract.py (fence parsing, incl.
the legacy search fence) and application/tools/orchestrator.py (the rounds
loop, idempotency and tool_call_log).
"""

from __future__ import annotations

import re

WEB_SEARCH_SKILL_KEY = "builtin-web-search"


def should_auto_search(text: str, patterns: list[str]) -> bool:
    """True when the user message carries a fresh-information intent signal.

    Pure-ASCII patterns are matched with word boundaries so ``now`` does not
    fire on ``know``; CJK patterns are plain substring matches.
    """
    if not text:
        return False
    for pattern in patterns:
        if not pattern:
            continue
        expression = rf"\b{re.escape(pattern)}\b" if pattern.isascii() else re.escape(pattern)
        if re.search(expression, text, re.IGNORECASE):
            return True
    return False


async def run_auto_search(*, system_blocks: list[dict], user_text: str, settings, event_stream, message_id: str, conversation_id: str | None) -> list[dict]:
    """Proactive one-shot search driven by intent patterns (not by the model).

    On a pattern hit: publishes ``message.searching``, runs one web_search
    (query = first 100 chars of the user message), and appends the results as
    a system context block. Search failure degrades to an error sentence
    inside the block — generation is never blocked. Does not count toward
    max_tool_rounds; called at most once per message by the worker.
    """
    from proseforge.infrastructure.search import SearchService

    patterns = list(getattr(settings, "search_auto_intent_patterns", None) or [])
    query = user_text.strip()[:100]
    if not patterns or not query or not should_auto_search(query, patterns):
        return system_blocks
    searching = {"event": "message.searching", "message_id": message_id, "query": query}
    await event_stream.publish(f"message:{message_id}", searching)
    if conversation_id:
        await event_stream.publish(f"conversation:{conversation_id}", searching)
    service = SearchService(
        engines=tuple(getattr(settings, "search_engines", None) or ("bing", "duckduckgo", "google", "yahoo", "brave", "mojeek", "ecosia", "startpage", "baidu")),
        timeout_seconds=float(getattr(settings, "search_timeout_seconds", 10.0)),
    )
    max_results = max(1, int(getattr(settings, "search_max_results", 5)))
    try:
        results = await service.web_search(query, max_results)
        lines = [f"{index}. [{item.title}]({item.url})\n   {item.snippet}" for index, item in enumerate(results, start=1)]
        body = "系统已主动联网搜索（可能存在误差，请甄别）：\n" + "\n".join(lines)
    except Exception as exc:
        body = f"系统已主动联网搜索\n\n自动搜索失败：{exc}"
    block = {
        "type": "tool",
        "source_type": "tool",
        "source_id": "builtin:web-search:auto",
        "text": body,
        # Post-snapshot block: no budget accounting, providers only read text.
        "token_estimate": 0,
        "priority": 70,
        "pinned": False,
        "redaction": False,
    }
    return [*system_blocks, block]


async def web_search_switch_enabled(uow, user_id: str) -> bool:
    """True when the user has an enabled state row for builtin-web-search."""
    if not user_id:
        return False
    states_repo = getattr(uow, "builtin_skill_states", None)
    if states_repo is None:
        return False
    return any(state.skill_key == WEB_SEARCH_SKILL_KEY and state.enabled for state in await states_repo.list_for_user(user_id))
