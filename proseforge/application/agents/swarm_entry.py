"""Swarm chat entry: one message becomes either a chat reply or an agent run.

Triggered by POST /api/v1/conversations/{id}/messages with mode="swarm" on
a work-mode project. Intent routing is two-phase:

1. Rule classifier (classify_intent) — write/review/revise hits are used
   immediately, zero latency.
2. Rule says "chat" -> the orchestrator (总调度) cluster model re-judges
   with a tiny non-streaming request (orchestrator_intent_prompt()). Any
   error, timeout (10s) or unparseable answer keeps "chat".

- chat   -> plain streaming reply via SendMessage, but the model is forced
            to the cluster write role (the request's provider/model are
            ignored in swarm); reasoning_level still honors the request.
- write/review/revise -> placeholder assistant message + an agent run from
            the intent's graph template, created through the SAME shared
            core as POST /agent-runs (create_agent_run: concurrency cap,
            idempotency, policy snapshot, enqueue). The run is linked to the
            assistant message (messages.agent_run_id); the executor writes
            the deterministic summary back when the run terminates.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Iterable

from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from proseforge.application.agents.create_run import RunTaskSpec, create_agent_run
from proseforge.application.agents.intent import (
    Intent,
    classify_intent,
    graph_for_intent,
    orchestrator_intent_prompt,
    parse_intent_answer,
)
from proseforge.application.conversations.send_message import SendMessage
from proseforge.application.models.cluster_config import RoleModels, resolve_role_models
from proseforge.domain.common.errors import NotFoundError
from proseforge.domain.dispatch import TaskPlan
from proseforge.infrastructure.database.models.chapter import ChapterModel
from proseforge.infrastructure.database.models.conversation import (
    ConversationBranchModel,
    ConversationModel,
)
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

logger = logging.getLogger(__name__)

# 12-task pipeline budget (plan->characters->3 parallel scene drafts->select
# ->3 reviews->merge->rewrite->recheck). Measured live at ~100K tokens per
# chapter; the quality-first passes (scene self-polish ~30K + one extra
# revise round after the final gate ~50K) plus the batch full-outline
# injection (~40K across 12 calls) push the measured average to ~180K with
# 240K peaks — 300K keeps the worst chapter clear of the wall.
DEFAULT_SWARM_BUDGET = 300000
# Second-pass classification must never stall message sending.
ORCHESTRATOR_CLASSIFY_TIMEOUT_SECONDS = 10.0

# Standalone revise/review runs need an existing chapter to work on; on an
# empty project they can only fail, so the entry answers inline instead.
_EMPTY_PROJECT_REPLY = "当前项目还没有任何章节，无法审校或改写。请先让我开始写作，或直接贴入完整大纲（我会先拆解成写作计划）。"


async def _project_has_chapters(uow: SqlAlchemyUnitOfWork, project_id: str) -> bool:
    count = await uow.session.scalar(select(func.count(ChapterModel.id)).where(ChapterModel.project_id == project_id))
    return bool(count)


async def _project_id_for_branch(uow: SqlAlchemyUnitOfWork, branch_id: str) -> str | None:
    return await uow.session.scalar(
        select(ConversationModel.project_id)
        .join(ConversationBranchModel, ConversationBranchModel.conversation_id == ConversationModel.id)
        .where(ConversationBranchModel.id == branch_id)
    )


async def _classify_with_orchestrator(
    uow_factory,
    *,
    user_id: str,
    roles: RoleModels,
    content: str,
    master_key: SecretStr,
) -> Intent | None:
    """Orchestrator-model second pass for rule-classified messages.

    Returns the parsed intent, or None on ANY failure (missing
    credential, provider error, timeout, unparseable answer) — the LLM
    second guess must never break message sending; callers substitute
    their own fallback ("chat" for the swarm entry, the rule result for
    the normal-mode dispatcher). The credential is read in its own short
    transaction; the model call happens outside any transaction (same
    discipline as the executor).
    """
    ref = roles.orchestrator or roles.write
    try:
        from proseforge.application.models.reasoning_policy import (
            resolve_task_reasoning,
        )
        from proseforge.application.models.resolve_model import (
            resolve_capabilities,
        )
        from proseforge.domain.ports.model_provider import GenerationRequest
        from proseforge.infrastructure.security.credential_cipher import (
            CredentialCipher,
            derive_key,
        )
        from proseforge.providers.factory import build_provider

        async with uow_factory() as uow:
            credential = await uow.credentials.get_for_user(user_id, ref[0])
            catalog_row = await uow.model_catalog.get(ref[0], ref[1])
            # Detach plain values before the short transaction closes.
            credential_id = credential.id if credential is not None else None
            encrypted_payload = credential.encrypted_payload if credential is not None else None
        if credential_id is None:
            return None
        associated = f"{user_id}:{ref[0]}:{credential_id}".encode()
        secret = json.loads(CredentialCipher(derive_key(master_key.get_secret_value())).decrypt(base64.b64decode(encrypted_payload), associated_data=associated))
        provider = build_provider(ref[0], str(secret["api_key"]), secret.get("base_url"))
        # Tiny classification answer: thinking OFF when the profile allows it,
        # else the lowest tier — a 16-token reply must never burn its budget
        # on reasoning (same elastic rule as the executor's JSON tasks).
        classification = resolve_task_reasoning("classify", "low", resolve_capabilities(catalog_row), 16)
        request = GenerationRequest(
            model=ref[1],
            system_blocks=({"role": "system", "text": orchestrator_intent_prompt()},),
            input_blocks=({"role": "user", "text": content},),
            max_output_tokens=int(classification["max_output"]),
            reasoning=classification["provider_parameter"],  # type: ignore[arg-type]
        )
        parts: list[str] = []

        async def _collect() -> None:
            async for event in provider.stream(request):
                if event.event == "content.delta":
                    parts.append(event.text)

        await asyncio.wait_for(_collect(), timeout=ORCHESTRATOR_CLASSIFY_TIMEOUT_SECONDS)
        return parse_intent_answer("".join(parts))
    except Exception:
        logger.warning("orchestrator intent classification failed", exc_info=True)
        return None


def _task_plan_for_intent(intent: Intent, *, project_id: str, goal: str) -> TaskPlan | None:
    """Assemble the classified intent into a schema-validated TaskPlan.

    The plan only references the existing graph template and registered
    roles; epoch is fixed at 1 (validate-and-pass-through only, no expiry
    rejection at this stage). Returns None on ANY validation failure so the
    caller falls back to the legacy graph_for_intent path — a TaskPlan
    schema problem must never change run-creation behavior.
    """
    try:
        return TaskPlan.model_validate({
            "intent": intent,
            "scope": {"project_id": project_id, "chapters": None},
            "tasks": [
                {
                    "id": str(item["id"]),
                    "role": str(item["role"]),
                    "goal": goal,
                    "depends_on": list(item.get("depends_on", ())),
                }
                for item in graph_for_intent(intent)
            ],
            "epoch": 1,
        })
    except (ValidationError, ValueError):
        logger.warning("TaskPlan validation failed for intent %r; using legacy graph path", intent, exc_info=True)
        return None


async def run_entry_response(
    uow_factory,
    queue,
    *,
    master_key: SecretStr,
    environment: str,
    branch_id: str,
    content: str,
    client_request_id: str,
    user_id: str,
    provider: str,
    model: str,
    intent: Intent,
    force_single_model: bool = False,
    attachment_ids: Iterable[str] = (),
    blob_root: str = "",
    settings=None,
) -> dict[str, object]:
    """Shared run-creation path: swarm entry and normal-mode dispatch.

    Appends the user message + PENDING assistant placeholder, builds task
    specs from the schema-validated TaskPlan (legacy graph template as
    fallback), and creates the run linked to the placeholder in one commit.
    force_single_model (normal mode) collapses every cluster lane onto the
    request's model via the run row's single_model marker. attachment_ids
    are pre-uploaded files: they link to the user message and their parsed
    text prefixes the run goal (the persisted message stays clean).
    """
    async with uow_factory() as uow:
        # Idempotent replay mirrors SendMessage's client_request_id dedupe:
        # a retried send replays the existing message pair, no second run.
        lock = getattr(uow.conversations, "lock_client_request", None)
        if lock is not None:
            await lock(client_request_id, user_id)
        existing = await uow.conversations.get_by_client_request_id(client_request_id, user_id)
        if existing is not None:
            assistant = await uow.conversations.assistant_after(existing.id)
            if assistant is not None:
                return {"user_message_id": existing.id, "assistant_message_id": assistant.id, "task_id": "deduplicated", "intent": intent, "agent_run_id": assistant.agent_run_id}
        project_id = await _project_id_for_branch(uow, branch_id)
        if project_id is None:
            raise NotFoundError("conversation or branch not found")
        user_message = await uow.conversations.append_message(branch_id, "user", content, client_request_id, "COMPLETED", user_id=user_id)
        goal = content
        if attachment_ids:
            # Link + inject in the same transaction that persists the user
            # message, so a worker can never read a half-linked state.
            from proseforge.application.files.message_attachments import (
                link_attachments_to_message,
                parse_attachment_blocks,
                prepend_blocks,
            )
            from proseforge.infrastructure.database.models.remaining import (
                AttachmentModel,
            )
            from proseforge.settings import get_settings

            ids = list(attachment_ids)
            await link_attachments_to_message(uow.session, ids, user_message.id)
            rows = (await uow.session.scalars(select(AttachmentModel).where(AttachmentModel.id.in_(ids)))).all()
            # blob_root comes from app.state.settings via the route; the
            # get_settings() fallback covers direct callers (tests, workers).
            goal = prepend_blocks(content, await parse_attachment_blocks(blob_root or get_settings().blob_root, rows))
        if intent in ("revise", "review") and not await _project_has_chapters(uow, project_id):
            # Empty project: a standalone revise/review run has no chapter to
            # work on and could only fail, so answer inline instead of
            # creating a run (the chief/review handlers keep their own
            # missing-chapter fallback as a second line of defense).
            assistant = await uow.conversations.append_message(branch_id, "assistant", _EMPTY_PROJECT_REPLY, None, "COMPLETED", parent_message_id=user_message.id)
            await uow.commit()
            return {"user_message_id": user_message.id, "assistant_message_id": assistant.id, "task_id": None, "intent": intent, "agent_run_id": None}
        assistant = await uow.conversations.append_message(branch_id, "assistant", "", None, "PENDING", parent_message_id=user_message.id)
        plan = _task_plan_for_intent(intent, project_id=project_id, goal=goal)
        if plan is not None:
            specs = [
                RunTaskSpec(id=task.id, role=task.role, depends_on=tuple(task.depends_on))
                for task in plan.tasks
            ]
        else:
            # Legacy fallback: identical to the pre-TaskPlan behavior.
            specs = [
                RunTaskSpec(id=str(item["id"]), role=str(item["role"]), depends_on=tuple(item.get("depends_on", ())))
                for item in graph_for_intent(intent)
            ]
        run, _created = await create_agent_run(
            uow, queue,
            user_id=user_id, project_id=project_id, goal=goal,
            tasks=specs, budget_limit=DEFAULT_SWARM_BUDGET,
            master_key=master_key, environment=environment,
            # Pass the caller's provider/model through; create_agent_run
            # keeps its own default fallback when they are None.
            provider=provider, model=model,
            # Link the placeholder assistant message inside the run's own
            # commit — one transaction, no run-without-message half state.
            assistant_message_id=assistant.id,
            force_single_model=force_single_model,
            settings=settings,
        )
        return {"user_message_id": user_message.id, "assistant_message_id": assistant.id, "task_id": run.id, "intent": intent, "agent_run_id": run.id}


async def handle_swarm_message(
    uow_factory,
    queue,
    *,
    master_key: SecretStr,
    environment: str,
    branch_id: str,
    content: str,
    client_request_id: str,
    user_id: str,
    provider: str,
    model: str,
    reasoning_level: str,
    attachment_ids: Iterable[str] = (),
    blob_root: str = "",
    settings=None,
) -> dict[str, object]:
    intent = classify_intent(content)

    roles: RoleModels | None = None
    if intent == "chat":
        # Rule missed it: ask the orchestrator model before settling on a
        # plain chat reply. Roles are resolved once and reused for the
        # write-model reply when the answer stays chat.
        async with uow_factory() as uow:
            project_id = await _project_id_for_branch(uow, branch_id)
            roles = await resolve_role_models(uow, user_id, locked=None, requested=(provider, model), project_id=project_id)
        intent = await _classify_with_orchestrator(uow_factory, user_id=user_id, roles=roles, content=content, master_key=master_key) or "chat"

    if intent == "chat":
        # Swarm chat still answers inline — but on the cluster write model.
        assert roles is not None  # resolved in the rule-chat branch above
        user_message, assistant, task_id = await SendMessage(uow_factory, queue).execute(
            branch_id=branch_id, content=content, client_request_id=client_request_id,
            user_id=user_id, provider=roles.write[0], model=roles.write[1],
            reasoning_level=reasoning_level, attachment_ids=attachment_ids,
        )
        return {"user_message_id": user_message.id, "assistant_message_id": assistant.id, "task_id": task_id, "intent": "chat", "agent_run_id": None}

    return await run_entry_response(
        uow_factory, queue,
        master_key=master_key, environment=environment,
        branch_id=branch_id, content=content,
        client_request_id=client_request_id, user_id=user_id,
        provider=provider, model=model, intent=intent,
        attachment_ids=attachment_ids, blob_root=blob_root,
        settings=settings,
    )
