from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from proseforge.domain.ports.model_provider import GenerationRequest, ModelProvider
from proseforge.domain.usage import UsageDelta
from proseforge.providers.usage import normalize_provider_usage

UsageObserver = Callable[[str, str, str, UsageDelta], Awaitable[None]]


async def generate_chapter_content(
    provider: ModelProvider,
    *,
    model: str,
    project_title: str,
    chapter_title: str,
    context_text: str = "",
    usage_call_id: str | None = None,
    on_usage: UsageObserver | None = None,
) -> str:
    """Collect one streamed Writer response without mutating persistence."""
    prompt = (
        f"Write a complete novel chapter titled {chapter_title!r} for the project {project_title!r}.\n"
        "Preserve continuity and do not include planning commentary.\n"
    )
    if context_text:
        prompt += f"Story context:\n{context_text}\n"
    request = GenerationRequest(
        model=model,
        system_blocks=({"role": "system", "text": "You are the Writer profile for ProseForge."},),
        input_blocks=({"role": "user", "text": prompt},),
        metadata={"workflow": "novel-generation", "role": "writer"},
    )
    content = await _collect(provider, request, role="writer", usage_call_id=usage_call_id, on_usage=on_usage)
    if not content:
        raise ValueError("writer provider returned empty chapter content")
    return content


REVIEW_SCHEMA = {
    "type": "object",
    "required": ["status", "summary", "issues", "preserve", "rewrite_scope"],
    "properties": {
        "status": {"type": "string", "enum": ["PASS", "WARN", "BLOCK"]},
        "summary": {"type": "string"},
        "issues": {"type": "array"},
        "preserve": {"type": "array"},
        "rewrite_scope": {"type": "array"},
    },
}


async def _collect(
    provider: ModelProvider,
    request: GenerationRequest,
    *,
    role: str,
    usage_call_id: str | None = None,
    on_usage: UsageObserver | None = None,
) -> str:
    parts: list[str] = []
    async for event in provider.stream(request):
        if event.event == "usage.updated" and on_usage is not None and usage_call_id:
            delta = normalize_provider_usage(str(getattr(provider, "provider_id", "unknown")), event.data, final=bool(event.data.get("final")))
            await on_usage(usage_call_id, str(getattr(provider, "provider_id", "unknown")), request.model, delta)
        if event.event == "content.delta":
            parts.append(event.text)
    return "".join(parts).strip()


async def review_chapter_content(provider: ModelProvider, *, model: str, content: str, usage_call_id: str | None = None, on_usage: UsageObserver | None = None) -> dict[str, object]:
    request = GenerationRequest(
        model=model,
        system_blocks=({"role": "system", "text": "You are the Editor profile. Return only valid JSON."},),
        input_blocks=({"role": "user", "text": f"Review this chapter and identify continuity, character, plot, prose, pacing, canon, and style issues.\n{content}"},),
        response_schema=REVIEW_SCHEMA,
        metadata={"workflow": "novel-generation", "role": "editor"},
    )
    raw = await _collect(provider, request, role="editor", usage_call_id=usage_call_id, on_usage=on_usage)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("editor returned invalid review JSON") from exc
    if not isinstance(result, dict) or str(result.get("status", "")).upper() not in {"PASS", "WARN", "BLOCK"}:
        raise ValueError("editor returned invalid review status")
    return result


async def rewrite_chapter_content(provider: ModelProvider, *, model: str, content: str, review: dict[str, object], usage_call_id: str | None = None, on_usage: UsageObserver | None = None) -> str:
    request = GenerationRequest(
        model=model,
        system_blocks=({"role": "system", "text": "You are the Editor profile. Rewrite only what the review requires."},),
        input_blocks=({"role": "user", "text": f"Original chapter:\n{content}\nReview JSON:\n{json.dumps(review, ensure_ascii=False)}"},),
        metadata={"workflow": "novel-generation", "role": "rewriter"},
    )
    rewritten = await _collect(provider, request, role="rewriter", usage_call_id=usage_call_id, on_usage=on_usage)
    if not rewritten:
        raise ValueError("editor returned empty rewrite")
    return rewritten


async def run_writer_editor_loop(provider: ModelProvider, *, writer_model: str, editor_model: str, project_title: str, chapter_title: str, context_text: str = "", max_rewrites: int = 2, usage_call_id_factory: Callable[[str], str] | None = None, on_usage: UsageObserver | None = None, editor_provider: ModelProvider | None = None, reviser_model: str | None = None, reviser_provider: ModelProvider | None = None) -> tuple[str, int, dict[str, object]]:
    # Cluster mode may split roles across providers; single-model runs pass
    # one provider and the defaults collapse to the pre-cluster behavior.
    review_provider = editor_provider or provider
    rewrite_provider = reviser_provider or review_provider
    rewrite_model = reviser_model or editor_model
    next_id = usage_call_id_factory or (lambda _role: "")
    content = await generate_chapter_content(provider, model=writer_model, project_title=project_title, chapter_title=chapter_title, context_text=context_text, usage_call_id=next_id("writer"), on_usage=on_usage)
    for rounds in range(max_rewrites + 1):
        review = await review_chapter_content(review_provider, model=editor_model, content=content, usage_call_id=next_id("editor"), on_usage=on_usage)
        status = str(review["status"]).upper()
        if status == "PASS":
            return content, rounds, review
        if rounds >= max_rewrites:
            raise ValueError("chapter blocked after maximum rewrite rounds")
        content = await rewrite_chapter_content(rewrite_provider, model=rewrite_model, content=content, review=review, usage_call_id=next_id("rewriter"), on_usage=on_usage)
    raise AssertionError("unreachable")
