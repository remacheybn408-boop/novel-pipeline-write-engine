"""Cluster (multi-model) configuration and role resolution.

Stored in user_preferences under the "cluster" key:
``{mode: "normal"|"cluster", write_model, review_model, revise_model,
orchestrator_model, analyst_model, reasoning}`` with role values as
"provider/model_id" strings or None (auto); ``reasoning`` holds the optional
思考强度 block ``{default, per_role}`` (see DEFAULT_CLUSTER_REASONING).
A project may override the global preference via
projects.cluster_config_json (same stored shape); resolution order is
project override > global > none (see get_effective_cluster_config).

Resolution rules:
- normal mode: every role uses the project's locked writing model when
  locked, otherwise the requested model.
- cluster mode: each role uses its configured model; None means auto —
  write falls back to locked, then to the requested model when it is
  still in the available pool, otherwise the first available model in
  the pool; review and revise auto-pick the first available model that
  is NOT the write model
  (a non-writer backup), falling back to the write model itself. An
  explicit ref that is no longer available (credential deleted, model
  gone from the catalog) degrades along the same path as an auto slot
  instead of failing at runtime.

Reasoning level is never part of MODEL resolution — it stays user-adjustable
per seat and is applied elastically per task at run time (reasoning_policy).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select

from proseforge.domain.model.capabilities import ReasoningLevel
from proseforge.infrastructure.database.models.plugin import UserPreferenceModel
from proseforge.infrastructure.database.models.project import ProjectModel

CLUSTER_PREF_KEY = "cluster"

# Cluster reasoning (思考强度) config: a global default level plus per-seat
# overrides, stored under the "reasoning" key of the same config dict. The
# defaults encode the agreed stance — orchestrator/analyst think at max, the
# writing seats at high. The level is a CEILING: the executor's elastic
# matrix (reasoning_policy.resolve_task_reasoning) downgrades per task type
# to protect small JSON output budgets.
REASONING_SEATS = ("orchestrator", "analyst", "write", "review", "revise")
_REASONING_LEVEL_VALUES = frozenset(level.value for level in ReasoningLevel)
DEFAULT_CLUSTER_REASONING: dict[str, object] = {
    "default": "high",
    "per_role": {"orchestrator": "max", "analyst": "max", "write": "high", "review": "high", "revise": "high"},
}

# Stored role fields scrubbed by scrub_stale_cluster_refs (the wire names
# live in the API layer; storage keeps the *_model keys).
_CLUSTER_ROLE_FIELDS = ("orchestrator_model", "analyst_model", "write_model", "review_model", "revise_model")

# Five-seat (五司) display aliases. The seat keys below are the storage/wire
# contract (DB cluster_config_json and the settings API) and never change;
# aliases are display-layer only (the Chinese 雅名 live in the web UI).
SEAT_ALIASES: dict[str, str] = {
    "orchestrator": "MARSHALL",
    "analyst": "HOLMES",
    "write": "SHAKESPEARE",
    "review": "JOHNSON",
    "revise": "MICHELANGELO",
}

DEFAULT_CLUSTER_CONFIG: dict[str, object] = {
    "mode": "normal",
    "write_model": None,
    "review_model": None,
    "revise_model": None,
    "orchestrator_model": None,
    "analyst_model": None,
    # Legacy rows predate this key; the shallow-merge fallback below fills it
    # (treat the nested dict as read-only — normalize_reasoning_config copies).
    "reasoning": DEFAULT_CLUSTER_REASONING,
}

ModelRef = tuple[str, str]  # (provider, model_id)


@dataclass(frozen=True)
class RoleModels:
    write: ModelRef
    review: ModelRef
    revise: ModelRef
    # 4th slot: the dispatcher model (swarm intent classification). Defaults
    # to None so every existing construction site keeps working; resolution
    # falls back to the write model when unconfigured.
    orchestrator: ModelRef | None = None
    # 5th slot: the analyst model (outline拆解). Unconfigured means "follow
    # the orchestrator slot"; resolve_from_config always fills it, so a None
    # here only appears in hand-built RoleModels.
    analyst: ModelRef | None = None


@dataclass(frozen=True)
class EffectiveClusterConfig:
    """Resolved cluster config: project override > global preference > none.

    source "none" means neither level configured anything (callers fall back
    to pre-cluster behavior); override marks a project-level row.
    """

    config: dict[str, object]
    source: str  # "project" | "global" | "none"
    override: bool


# Agent task role -> cluster role. The V3 executor (swarm) picks the model
# per task through this mapping; write covers every planning/writing role,
# review the two reviewer roles, revise the editors, and analyst gets its own
# seat (unconfigured it resolves to the orchestrator slot's model — the same
# model pre-seat configs used). Unknown roles fall back to write — they
# produce primary content by default.
_AGENT_ROLE_TO_CLUSTER_ROLE: dict[str, str] = {
    "chief_planner": "write",
    "story_architect": "write",
    "world_builder": "write",
    "character_designer": "write",
    "timeline_analyst": "write",
    "scene_writer": "write",
    "continuity_reviewer": "review",
    "adversarial_reviewer": "review",
    "style_editor": "revise",
    "merge_editor": "revise",
    "chief_editor": "revise",
    "analyst": "analyst",
    # promise_keeper has no seat of its own: it follows the orchestrator
    # model (the executor's five seats already include orchestrator).
    "promise_keeper": "orchestrator",
}


def agent_role_to_cluster_role(role: str, task_key: str = "") -> str:
    """Map a V3 agent task to a cluster role (write/review/revise/orchestrator/analyst).

    task_key wins over the role when given: the same role can serve
    different stages in the pipeline graph (e.g. continuity_reviewer is a
    review-stage task as review_continuity but the revise-stage recheck).
    The default (no task_key) keeps the pre-pipeline behavior.
    """
    if task_key:
        if task_key.startswith("review_"):
            return "review"
        if task_key == "recheck" or task_key.startswith(("merge", "rewrite", "chief")):
            return "revise"
    return _AGENT_ROLE_TO_CLUSTER_ROLE.get(role, "write")


def parse_model_ref(value: object) -> ModelRef | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    provider, model_id = value.split("/", 1)
    if not provider or not model_id:
        return None
    return provider, model_id


def normalize_reasoning_config(raw: object) -> dict[str, object]:
    """Stored reasoning block -> full ``{default, per_role}`` shape (fresh dicts).

    Lenient read path, never raises: a missing block (legacy rows predate the
    field) yields the agreed defaults verbatim; a present block falls back
    per value — an invalid default becomes the default default, a seat
    without a valid override inherits the (effective) default. The settings
    API validates strictly on write; this is the read/executor path.
    """
    if not isinstance(raw, dict):
        return {"default": str(DEFAULT_CLUSTER_REASONING["default"]), "per_role": dict(DEFAULT_CLUSTER_REASONING["per_role"])}  # type: ignore[arg-type]
    default = raw.get("default")
    if default not in _REASONING_LEVEL_VALUES:
        default = DEFAULT_CLUSTER_REASONING["default"]
    stored = raw.get("per_role")
    stored = stored if isinstance(stored, dict) else {}
    per_role: dict[str, str] = {}
    for seat in REASONING_SEATS:
        level = stored.get(seat)
        per_role[seat] = str(level) if level in _REASONING_LEVEL_VALUES else str(default)
    return {"default": str(default), "per_role": per_role}


def reasoning_level_for(reasoning: object, cluster_role: str) -> str:
    """Effective level for a seat: its per_role override, else the default."""
    config = normalize_reasoning_config(reasoning)
    per_role = config["per_role"]
    assert isinstance(per_role, dict)
    return str(per_role.get(cluster_role, config["default"]))


async def get_cluster_config(uow, user_id: str) -> dict[str, object]:
    preference = await uow.user_preferences.get(user_id, CLUSTER_PREF_KEY)
    if preference is None:
        return dict(DEFAULT_CLUSTER_CONFIG)
    try:
        stored = json.loads(preference.value_json)
    except (TypeError, ValueError):
        return dict(DEFAULT_CLUSTER_CONFIG)
    return {**DEFAULT_CLUSTER_CONFIG, **stored}


async def get_effective_cluster_config(uow, user_id: str, project_id: str | None) -> EffectiveClusterConfig:
    """Project override > global preference > none (pre-cluster fallback).

    A project row that fails to parse counts as absent (falls through to
    global); a missing global preference row is source "none" even though
    the returned config equals DEFAULT_CLUSTER_CONFIG.
    """
    if project_id:
        raw = await uow.session.scalar(
            select(ProjectModel.cluster_config_json).where(ProjectModel.id == project_id)
        )
        if raw:
            try:
                stored = json.loads(raw)
            except (TypeError, ValueError):
                stored = None
            if isinstance(stored, dict):
                return EffectiveClusterConfig(config={**DEFAULT_CLUSTER_CONFIG, **stored}, source="project", override=True)
    preference = await uow.user_preferences.get(user_id, CLUSTER_PREF_KEY)
    if preference is None:
        return EffectiveClusterConfig(config=dict(DEFAULT_CLUSTER_CONFIG), source="none", override=False)
    return EffectiveClusterConfig(config=await get_cluster_config(uow, user_id), source="global", override=False)


async def available_model_refs(uow, user_id: str) -> list[ModelRef]:
    """Available catalog models on providers the user has credentials for,
    sorted for deterministic auto-picks."""
    credentials = await uow.credentials.list_for_user(user_id)
    providers = {credential.provider for credential in credentials}
    if not providers:
        return []
    models = await uow.model_catalog.list(available_only=True)
    return sorted(
        {(model.provider, model.model_id) for model in models if model.provider in providers}
    )


def scrub_stale_refs(config: dict[str, object], pool: list[ModelRef]) -> dict[str, object] | None:
    """Copy of config with role refs that fell out of the available pool
    reset to None (auto); None when nothing needed changing."""
    available = set(pool)
    cleaned = dict(config)
    changed = False
    for field in _CLUSTER_ROLE_FIELDS:
        ref = parse_model_ref(cleaned.get(field))
        if ref is not None and ref not in available:
            cleaned[field] = None
            changed = True
    return cleaned if changed else None


async def _scrub_user_cluster_refs(uow, user_id: str) -> int:
    pool = await available_model_refs(uow, user_id)
    changed = 0
    preference = await uow.user_preferences.get(user_id, CLUSTER_PREF_KEY)
    if preference is not None:
        try:
            stored = json.loads(preference.value_json)
        except (TypeError, ValueError):
            stored = None
        if isinstance(stored, dict):
            cleaned = scrub_stale_refs({**DEFAULT_CLUSTER_CONFIG, **stored}, pool)
            if cleaned is not None:
                await uow.user_preferences.set(user_id, CLUSTER_PREF_KEY, json.dumps(cleaned))
                changed += 1
    rows = (
        await uow.session.scalars(
            select(ProjectModel).where(
                ProjectModel.owner_id == user_id,
                ProjectModel.cluster_config_json.is_not(None),
            )
        )
    ).all()
    for row in rows:
        try:
            parsed = json.loads(row.cluster_config_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        cleaned = scrub_stale_refs({**DEFAULT_CLUSTER_CONFIG, **parsed}, pool)
        if cleaned is not None:
            row.cluster_config_json = json.dumps(cleaned)
            changed += 1
    return changed


async def scrub_stale_cluster_refs(uow, user_id: str | None) -> int:
    """Reset cluster role refs pointing at no-longer-available models back to
    auto — the settings-panel counterpart of resolve_from_config's runtime
    degradation, so deleting a model or credential never leaves the cluster
    config unsavable (PUT rejects refs outside the pool). Pass user_id=None
    to scrub every user (used when a provider loses its last credential
    anywhere). Runs inside the caller's transaction; no commit here."""
    if user_id is not None:
        return await _scrub_user_cluster_refs(uow, user_id)
    user_ids = set(
        (
            await uow.session.scalars(
                select(UserPreferenceModel.user_id).where(UserPreferenceModel.key == CLUSTER_PREF_KEY)
            )
        ).all()
    )
    user_ids.update(
        (
            await uow.session.scalars(
                select(ProjectModel.owner_id).where(ProjectModel.cluster_config_json.is_not(None))
            )
        ).all()
    )
    changed = 0
    for affected_user_id in user_ids:
        changed += await _scrub_user_cluster_refs(uow, affected_user_id)
    return changed


def resolve_from_config(
    config: dict[str, object],
    *,
    pool: list[ModelRef],
    locked: ModelRef | None,
    requested: ModelRef,
) -> RoleModels:
    """Pure role resolution against an already-loaded config + model pool."""
    if config.get("mode") != "cluster":
        effective = locked or requested
        return RoleModels(write=effective, review=effective, revise=effective, orchestrator=effective, analyst=effective)

    def explicit(value: object, fallback: ModelRef) -> ModelRef:
        # An explicit slot whose ref is no longer available (its provider
        # credential was deleted or the model left the catalog) degrades
        # along the same path as an auto slot instead of failing at run
        # time; with an empty pool there is nothing to validate against,
        # so the configured ref stands as before.
        ref = parse_model_ref(value)
        if ref is None or (pool and ref not in pool):
            return fallback
        return ref

    if pool:
        # Pool-first fallback: a locked model always wins; the requested
        # model is honored only while it is actually runnable (its
        # credential may have been deleted mid-flight, e.g. headless run
        # creation), otherwise the first available pool model takes over —
        # an unrunnable write ref would fail the run at executor time
        # despite usable models sitting in the pool.
        write_fallback = locked or (requested if requested in pool else pool[0])
    else:
        # Empty pool: nothing to validate against, keep the legacy ref.
        write_fallback = locked or requested
    write = explicit(config.get("write_model"), write_fallback)
    # Orchestrator slot: unconfigured means "follow the write model".
    orchestrator = explicit(config.get("orchestrator_model"), write)
    # Analyst slot: unconfigured means "follow the orchestrator model"
    # (exactly where analyst tasks ran before the seat existed).
    analyst = explicit(config.get("analyst_model"), orchestrator)
    if not pool:
        return RoleModels(write=write, review=write, revise=write, orchestrator=orchestrator, analyst=analyst)

    def backup(exclude: ModelRef) -> ModelRef:
        # Auto review/revise: prefer a non-writer backup model.
        return next((ref for ref in pool if ref != exclude), exclude)

    review = explicit(config.get("review_model"), backup(write))
    revise = explicit(config.get("revise_model"), backup(write))
    return RoleModels(write=write, review=review, revise=revise, orchestrator=orchestrator, analyst=analyst)


async def resolve_role_models(
    uow,
    user_id: str,
    *,
    locked: ModelRef | None,
    requested: ModelRef,
    project_id: str | None = None,
) -> RoleModels:
    effective = await get_effective_cluster_config(uow, user_id, project_id)
    if effective.config.get("mode") != "cluster":
        # Normal mode never touches credentials/catalog (short-circuit kept:
        # some callers run against schemas without those tables).
        return resolve_from_config(effective.config, pool=[], locked=locked, requested=requested)
    pool = await available_model_refs(uow, user_id)
    return resolve_from_config(effective.config, pool=pool, locked=locked, requested=requested)
