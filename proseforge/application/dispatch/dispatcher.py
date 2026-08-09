"""Unified message dispatch: normal and swarm modes share one pipeline.

Normal mode (no ``mode`` field on the request) enters here instead of going
straight to SendMessage. The rule classifier runs with zero extra model
calls, so plain chat latency is unchanged:

- chat intent (or a non-work project) -> plain streaming reply via
  SendMessage, byte-for-byte the pre-dispatcher behavior.
- write/review/revise/analyze on a work project -> the orchestrator model
  re-judges the rule hit first (question-shaped messages like
  "帮我看看这个大纲怎么样" match writing keywords and must not trigger a
  run): an explicit chat verdict replies inline, another work verdict
  creates the run from THAT intent, and any failure/timeout keeps the
  rule result (availability first). The run goes through the same shared
  core as the swarm entry, with every cluster lane collapsed onto the
  user's selected model (force_single_model): normal mode is the
  five-seat architecture degenerated to one model.

The swarm entry (mode="swarm") keeps its own two-phase classification
with the orchestrator model; the LLM second pass only runs on rule hits
here, so plain chat latency is unchanged (zero extra model calls).
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import SecretStr

from proseforge.application.agents.intent import classify_intent
from proseforge.application.agents.swarm_entry import (
    _classify_with_orchestrator,
    run_entry_response,
)
from proseforge.application.conversations.send_message import SendMessage
from proseforge.application.models.cluster_config import RoleModels


async def dispatch_normal_message(
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
    project_mode: str | None,
    attachment_ids: Iterable[str] = (),
    blob_root: str = "",
) -> dict[str, object]:
    """Normal-mode dispatch: rule classify -> orchestrator confirm -> direct reply or collapsed run."""
    intent = classify_intent(content) if project_mode == "work" else "chat"
    if intent != "chat":
        # Rule hits on work keywords also fire on questions ABOUT the work
        # (a message containing 大纲 is not necessarily a writing request),
        # which must not trigger a collapsed 12-task run. Re-judge with the
        # orchestrator model: normal mode has no cluster, so every role
        # slot is the user's selected model (the same collapse semantics
        # the run itself uses). None means timeout/any failure -> keep the
        # rule result, availability first.
        ref = (provider, model)
        roles = RoleModels(write=ref, review=ref, revise=ref, orchestrator=ref, analyst=ref)
        judged = await _classify_with_orchestrator(
            uow_factory, user_id=user_id, roles=roles, content=content, master_key=master_key
        )
        if judged is not None:
            intent = judged
    if intent == "chat":
        user_message, assistant, task_id = await SendMessage(uow_factory, queue).execute(
            branch_id=branch_id, content=content, client_request_id=client_request_id,
            user_id=user_id, provider=provider, model=model,
            reasoning_level=reasoning_level, attachment_ids=attachment_ids,
        )
        return {"user_message_id": user_message.id, "assistant_message_id": assistant.id, "task_id": task_id}
    return await run_entry_response(
        uow_factory, queue,
        master_key=master_key, environment=environment,
        branch_id=branch_id, content=content,
        client_request_id=client_request_id, user_id=user_id,
        provider=provider, model=model, intent=intent,
        force_single_model=True, attachment_ids=attachment_ids, blob_root=blob_root,
    )
