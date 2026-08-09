"""resolve_role_models: normal vs cluster mode role resolution."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from proseforge.application.models.cluster_config import (
    RoleModels,
    available_model_refs,
    parse_model_ref,
    resolve_role_models,
)


class _FakePreferences:
    def __init__(self, config: dict | None):
        self._config = config

    async def get(self, user_id: str, key: str):
        if self._config is None:
            return None
        return SimpleNamespace(value_json=json.dumps(self._config))


class _FakeCredentials:
    def __init__(self, providers: list[str]):
        self._providers = providers

    async def list_for_user(self, user_id: str):
        return [SimpleNamespace(provider=provider) for provider in self._providers]


class _FakeCatalog:
    def __init__(self, models: list[tuple[str, str]]):
        self._models = models

    async def list(self, provider=None, search=None, available_only=False):
        return [SimpleNamespace(provider=p, model_id=m) for p, m in self._models]


def _uow(config: dict | None, providers: list[str], models: list[tuple[str, str]]):
    return SimpleNamespace(
        user_preferences=_FakePreferences(config),
        credentials=_FakeCredentials(providers),
        model_catalog=_FakeCatalog(models),
    )


def test_parse_model_ref():
    assert parse_model_ref("openai/gpt-4o") == ("openai", "gpt-4o")
    assert parse_model_ref("deepseek/deepseek-chat") == ("deepseek", "deepseek-chat")
    assert parse_model_ref(None) is None
    assert parse_model_ref("no-slash") is None
    assert parse_model_ref("/model") is None
    assert parse_model_ref("provider/") is None


@pytest.mark.asyncio
async def test_normal_mode_locked_wins_for_all_roles():
    uow = _uow(None, ["openai"], [("openai", "a")])
    roles = await resolve_role_models(uow, "u1", locked=("openai", "locked-model"), requested=("openai", "requested"))
    assert roles == RoleModels(
        ("openai", "locked-model"), ("openai", "locked-model"), ("openai", "locked-model"), ("openai", "locked-model"), ("openai", "locked-model")
    )


@pytest.mark.asyncio
async def test_normal_mode_unlocked_uses_requested():
    uow = _uow({"mode": "normal", "write_model": None, "review_model": None, "revise_model": None}, ["openai"], [("openai", "a")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "requested"))
    assert roles.write == ("openai", "requested")
    assert roles.review == ("openai", "requested")
    assert roles.revise == ("openai", "requested")


@pytest.mark.asyncio
async def test_cluster_mode_explicit_role_models():
    config = {"mode": "cluster", "write_model": "openai/w", "review_model": "deepseek/r", "revise_model": "deepseek/v"}
    uow = _uow(config, ["openai", "deepseek"], [("openai", "w"), ("deepseek", "r"), ("deepseek", "v")])
    roles = await resolve_role_models(uow, "u1", locked=("openai", "locked"), requested=("openai", "requested"))
    # orchestrator unset -> write; analyst unset -> orchestrator (-> write).
    assert roles == RoleModels(("openai", "w"), ("deepseek", "r"), ("deepseek", "v"), ("openai", "w"), ("openai", "w"))


@pytest.mark.asyncio
async def test_cluster_mode_null_roles_auto_pick():
    # write null -> locked fallback; review/revise null -> first available
    # model that is NOT the write model (non-writer backup).
    config = {"mode": "cluster", "write_model": None, "review_model": None, "revise_model": None}
    uow = _uow(config, ["openai", "deepseek"], [("openai", "w"), ("deepseek", "chat")])
    roles = await resolve_role_models(uow, "u1", locked=("openai", "w"), requested=("openai", "w"))
    assert roles.write == ("openai", "w")
    assert roles.review == ("deepseek", "chat")
    assert roles.revise == ("deepseek", "chat")


@pytest.mark.asyncio
async def test_cluster_mode_single_model_pool_falls_back_to_write():
    config = {"mode": "cluster", "write_model": "openai/w", "review_model": None, "revise_model": None}
    uow = _uow(config, ["openai"], [("openai", "w")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "w"))
    assert roles.review == ("openai", "w")
    assert roles.revise == ("openai", "w")


@pytest.mark.asyncio
async def test_cluster_mode_empty_pool_uses_write_everywhere():
    config = {"mode": "cluster", "write_model": None, "review_model": None, "revise_model": None}
    uow = _uow(config, [], [])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "requested"))
    assert roles == RoleModels(
        ("openai", "requested"), ("openai", "requested"), ("openai", "requested"), ("openai", "requested"), ("openai", "requested")
    )


@pytest.mark.asyncio
async def test_available_model_refs_filters_by_credential_providers():
    uow = _uow(None, ["openai"], [("openai", "a"), ("openai", "b"), ("deepseek", "chat")])
    refs = await available_model_refs(uow, "u1")
    assert refs == [("openai", "a"), ("openai", "b")]


# ---------------------------------------------------------------------------
# agent_role_to_cluster_role + get_effective_cluster_config (project override)
# ---------------------------------------------------------------------------

from proseforge.application.models.cluster_config import (
    SEAT_ALIASES,
    agent_role_to_cluster_role,
    get_effective_cluster_config,
    resolve_from_config,
)


def test_seat_aliases_cover_the_five_seats():
    assert SEAT_ALIASES == {
        "orchestrator": "MARSHALL",
        "analyst": "HOLMES",
        "write": "SHAKESPEARE",
        "review": "JOHNSON",
        "revise": "MICHELANGELO",
    }


def test_agent_role_to_cluster_role_full_mapping():
    for role in ("chief_planner", "story_architect", "world_builder", "character_designer", "timeline_analyst", "scene_writer"):
        assert agent_role_to_cluster_role(role) == "write"
    for role in ("continuity_reviewer", "adversarial_reviewer"):
        assert agent_role_to_cluster_role(role) == "review"
    for role in ("style_editor", "merge_editor", "chief_editor"):
        assert agent_role_to_cluster_role(role) == "revise"


def test_agent_role_to_cluster_role_unknown_falls_back_to_write():
    assert agent_role_to_cluster_role("brand_new_role") == "write"
    assert agent_role_to_cluster_role("") == "write"


def test_agent_role_to_cluster_role_analyst_has_own_seat():
    # The analyst maps to its own seat; resolution falls back to the
    # orchestrator model when the analyst slot is unconfigured.
    assert agent_role_to_cluster_role("analyst") == "analyst"
    assert agent_role_to_cluster_role("analyst", "analyze") == "analyst"


def test_agent_role_to_cluster_role_task_key_wins():
    # review_* prefix -> review lane, regardless of the role's own mapping.
    assert agent_role_to_cluster_role("style_editor", "review_style") == "review"
    assert agent_role_to_cluster_role("continuity_reviewer", "review_continuity") == "review"
    # revise-stage keys -> revise lane (recheck reuses continuity_reviewer).
    assert agent_role_to_cluster_role("merge_editor", "merge") == "revise"
    assert agent_role_to_cluster_role("chief_editor", "rewrite") == "revise"
    assert agent_role_to_cluster_role("continuity_reviewer", "recheck") == "revise"
    assert agent_role_to_cluster_role("chief_editor", "chief") == "revise"
    # writing stage keeps the role mapping.
    assert agent_role_to_cluster_role("scene_writer", "scene") == "write"
    assert agent_role_to_cluster_role("character_designer", "character") == "write"


class _FakeSession:
    """Only what get_effective_cluster_config touches: scalar(project lookup)."""

    def __init__(self, cluster_config_json: str | None):
        self._raw = cluster_config_json

    async def scalar(self, _query):
        return self._raw


def _uow_with_project(config: dict | None, cluster_json: str | None, providers=None, models=None):
    return SimpleNamespace(
        user_preferences=_FakePreferences(config),
        credentials=_FakeCredentials(providers or []),
        model_catalog=_FakeCatalog(models or []),
        session=_FakeSession(cluster_json),
    )


@pytest.mark.asyncio
async def test_effective_config_project_override_wins():
    project_stored = json.dumps({"mode": "cluster", "write_model": "openai/proj-w"})
    global_config = {"mode": "cluster", "write_model": "openai/global-w"}
    uow = _uow_with_project(global_config, project_stored)

    effective = await get_effective_cluster_config(uow, "u1", "p1")

    assert effective.source == "project" and effective.override is True
    assert effective.config["write_model"] == "openai/proj-w"


@pytest.mark.asyncio
async def test_effective_config_falls_back_to_global():
    uow = _uow_with_project({"mode": "cluster", "write_model": "openai/global-w"}, None)

    effective = await get_effective_cluster_config(uow, "u1", "p1")

    assert effective.source == "global" and effective.override is False
    assert effective.config["write_model"] == "openai/global-w"


@pytest.mark.asyncio
async def test_effective_config_invalid_project_json_falls_back_to_global():
    uow = _uow_with_project({"mode": "normal"}, "{not json")

    effective = await get_effective_cluster_config(uow, "u1", "p1")

    assert effective.source == "global"


@pytest.mark.asyncio
async def test_effective_config_none_when_nothing_configured():
    uow = _uow_with_project(None, None)

    effective = await get_effective_cluster_config(uow, "u1", "p1")

    assert effective.source == "none" and effective.override is False
    assert effective.config["mode"] == "normal"


@pytest.mark.asyncio
async def test_resolve_role_models_honors_project_override():
    project_stored = json.dumps({"mode": "cluster", "write_model": "openai/proj-w", "review_model": "deepseek/proj-r"})
    global_config = {"mode": "cluster", "write_model": "openai/global-w"}
    uow = _uow_with_project(global_config, project_stored, ["openai", "deepseek"], [("openai", "proj-w"), ("openai", "global-w"), ("deepseek", "proj-r")])

    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "requested"), project_id="p1")

    assert roles.write == ("openai", "proj-w")
    assert roles.review == ("deepseek", "proj-r")


def test_resolve_from_config_pure_resolution():
    config = {"mode": "cluster", "write_model": "openai/w", "review_model": None, "revise_model": None}
    roles = resolve_from_config(config, pool=[("openai", "w"), ("deepseek", "d")], locked=None, requested=("openai", "req"))
    assert roles.write == ("openai", "w")
    assert roles.review == ("deepseek", "d")  # auto review picks the non-writer backup


@pytest.mark.asyncio
async def test_orchestrator_unset_follows_write_model():
    config = {"mode": "cluster", "write_model": "openai/w"}
    uow = _uow(config, ["openai"], [("openai", "w")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "w"))
    assert roles.orchestrator == ("openai", "w")


@pytest.mark.asyncio
async def test_orchestrator_explicit_model_wins():
    config = {"mode": "cluster", "write_model": "openai/w", "orchestrator_model": "deepseek/d"}
    uow = _uow(config, ["openai", "deepseek"], [("openai", "w"), ("deepseek", "d")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "w"))
    assert roles.orchestrator == ("deepseek", "d")
    assert roles.write == ("openai", "w")  # other slots unaffected


@pytest.mark.asyncio
async def test_orchestrator_normal_mode_is_effective():
    uow = _uow(None, [], [])
    roles = await resolve_role_models(uow, "u1", locked=("openai", "locked"), requested=("openai", "requested"))
    assert roles.orchestrator == ("openai", "locked")


def test_resolve_from_config_orchestrator_pure():
    config = {"mode": "cluster", "write_model": "openai/w", "orchestrator_model": "deepseek/d"}
    roles = resolve_from_config(config, pool=[("openai", "w"), ("deepseek", "d")], locked=None, requested=("openai", "r"))
    assert roles.orchestrator == ("deepseek", "d")
    # Old configs without the key behave exactly like "unset".
    legacy = {"mode": "cluster", "write_model": "openai/w"}
    roles = resolve_from_config(legacy, pool=[("openai", "w")], locked=None, requested=("openai", "r"))
    assert roles.orchestrator == ("openai", "w")


@pytest.mark.asyncio
async def test_analyst_unset_follows_orchestrator_model():
    config = {"mode": "cluster", "write_model": "openai/w", "orchestrator_model": "deepseek/d"}
    uow = _uow(config, ["openai", "deepseek"], [("openai", "w"), ("deepseek", "d")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "w"))
    assert roles.analyst == ("deepseek", "d")  # unconfigured analyst follows orchestrator


@pytest.mark.asyncio
async def test_analyst_explicit_model_wins():
    config = {"mode": "cluster", "write_model": "openai/w", "orchestrator_model": "deepseek/d", "analyst_model": "anthropic/a"}
    uow = _uow(config, ["openai", "deepseek", "anthropic"], [("openai", "w"), ("deepseek", "d"), ("anthropic", "a")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "w"))
    assert roles.analyst == ("anthropic", "a")
    assert roles.orchestrator == ("deepseek", "d")  # other slots unaffected


@pytest.mark.asyncio
async def test_analyst_normal_mode_is_effective():
    uow = _uow(None, [], [])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "requested"))
    assert roles.analyst == ("openai", "requested")


def test_resolve_from_config_analyst_pure():
    config = {"mode": "cluster", "write_model": "openai/w", "analyst_model": "deepseek/a"}
    roles = resolve_from_config(config, pool=[("openai", "w"), ("deepseek", "a")], locked=None, requested=("openai", "r"))
    assert roles.analyst == ("deepseek", "a")
    # Old configs without the key behave exactly like "unset" (follow
    # orchestrator, which itself follows write).
    legacy = {"mode": "cluster", "write_model": "openai/w"}
    roles = resolve_from_config(legacy, pool=[("openai", "w")], locked=None, requested=("openai", "r"))
    assert roles.analyst == ("openai", "w")


# ---------------------------------------------------------------------------
# Stale explicit refs (credential deleted after configuration) degrade like
# auto slots instead of failing at run time.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cluster_mode_stale_review_model_degrades_to_auto_backup():
    # review_model still points at deepseek/r but the deepseek credential is
    # gone -> falls back to the auto path (first available non-writer model).
    config = {"mode": "cluster", "write_model": "openai/w", "review_model": "deepseek/r", "revise_model": None}
    uow = _uow(config, ["openai", "anthropic"], [("openai", "w"), ("anthropic", "a")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "w"))
    assert roles.write == ("openai", "w")
    assert roles.review == ("anthropic", "a")
    assert roles.revise == ("anthropic", "a")


@pytest.mark.asyncio
async def test_cluster_mode_stale_write_model_degrades_to_locked_or_requested():
    # The explicit write model lost its credential -> same fallback as an
    # unset write slot: locked first, otherwise requested.
    config = {"mode": "cluster", "write_model": "deepseek/w", "review_model": None, "revise_model": None}
    uow = _uow(config, ["openai"], [("openai", "a")])
    roles = await resolve_role_models(uow, "u1", locked=("openai", "locked"), requested=("openai", "requested"))
    assert roles.write == ("openai", "locked")

    uow = _uow(config, ["openai"], [("openai", "a")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "a"))
    assert roles.write == ("openai", "a")  # requested still in the pool


@pytest.mark.asyncio
async def test_cluster_mode_write_fallback_prefers_pool_over_unrunnable_requested():
    # requested has no credential (deleted mid-flight / headless run):
    # the first available pool model takes over instead of failing the
    # run at executor time.
    config = {"mode": "cluster", "write_model": None, "review_model": None, "revise_model": None}
    uow = _uow(config, ["openai"], [("openai", "a"), ("openai", "b")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("deepseek", "gone"))
    assert roles.write == ("openai", "a")


@pytest.mark.asyncio
async def test_cluster_mode_write_fallback_keeps_requested_when_in_pool():
    config = {"mode": "cluster", "write_model": None, "review_model": None, "revise_model": None}
    uow = _uow(config, ["openai"], [("openai", "a"), ("openai", "b")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "b"))
    assert roles.write == ("openai", "b")


def test_resolve_from_config_write_pool_first_pure():
    config = {"mode": "cluster", "write_model": None}
    pool = [("openai", "a"), ("openai", "b")]
    # requested unrunnable -> pool[0]; locked still wins over everything.
    roles = resolve_from_config(config, pool=pool, locked=None, requested=("deepseek", "gone"))
    assert roles.write == ("openai", "a")
    roles = resolve_from_config(config, pool=pool, locked=("anthropic", "l"), requested=("deepseek", "gone"))
    assert roles.write == ("anthropic", "l")
    # Empty pool keeps the legacy locked/requested behavior.
    roles = resolve_from_config(config, pool=[], locked=None, requested=("deepseek", "gone"))
    assert roles.write == ("deepseek", "gone")


@pytest.mark.asyncio
async def test_cluster_mode_stale_orchestrator_model_degrades_to_write():
    config = {"mode": "cluster", "write_model": "openai/w", "orchestrator_model": "deepseek/d"}
    uow = _uow(config, ["openai"], [("openai", "w")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "w"))
    assert roles.orchestrator == ("openai", "w")


@pytest.mark.asyncio
async def test_cluster_mode_stale_analyst_model_degrades_to_orchestrator():
    config = {"mode": "cluster", "write_model": "openai/w", "orchestrator_model": "openai/o", "analyst_model": "deepseek/a"}
    uow = _uow(config, ["openai"], [("openai", "w"), ("openai", "o")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "w"))
    assert roles.analyst == ("openai", "o")


@pytest.mark.asyncio
async def test_cluster_mode_stale_explicit_refs_never_leave_the_pool():
    # Every resolved role model must be runnable: with credentials only for
    # openai, no stale deepseek ref may survive resolution.
    config = {
        "mode": "cluster",
        "write_model": "deepseek/w",
        "review_model": "deepseek/r",
        "revise_model": "deepseek/v",
        "orchestrator_model": "deepseek/o",
        "analyst_model": "deepseek/a",
    }
    uow = _uow(config, ["openai"], [("openai", "a"), ("openai", "b")])
    roles = await resolve_role_models(uow, "u1", locked=None, requested=("openai", "a"))
    pool = {("openai", "a"), ("openai", "b")}
    for ref in (roles.write, roles.review, roles.revise, roles.orchestrator, roles.analyst):
        assert ref in pool


def test_resolve_from_config_empty_pool_keeps_explicit_refs():
    # With no pool at all there is nothing to validate against (and no
    # fallback to pick), so explicit refs stand as before.
    config = {"mode": "cluster", "write_model": "deepseek/w", "review_model": "deepseek/r"}
    roles = resolve_from_config(config, pool=[], locked=None, requested=("openai", "req"))
    assert roles.write == ("deepseek", "w")
    assert roles.review == ("deepseek", "w")  # empty pool -> write everywhere
    assert roles.orchestrator == ("deepseek", "w")
    assert roles.analyst == ("deepseek", "w")


# ---------------------------------------------------------------------------
# reasoning (思考强度) block: normalize + per-seat resolution
# ---------------------------------------------------------------------------

from proseforge.application.models.cluster_config import (
    DEFAULT_CLUSTER_REASONING,
    get_cluster_config,
    normalize_reasoning_config,
    reasoning_level_for,
)


def test_normalize_reasoning_config_absent_block_yields_agreed_defaults():
    # Legacy rows predate the field: the shipped stance applies verbatim —
    # orchestrator/analyst at max, the writing seats at high.
    assert normalize_reasoning_config(None) == {
        "default": "high",
        "per_role": {"orchestrator": "max", "analyst": "max", "write": "high", "review": "high", "revise": "high"},
    }
    assert normalize_reasoning_config("garbage") == normalize_reasoning_config(None)


def test_normalize_reasoning_config_fills_seats_from_default():
    # A seat without an explicit override inherits the effective default.
    assert normalize_reasoning_config({"default": "low", "per_role": {"write": "max"}}) == {
        "default": "low",
        "per_role": {"orchestrator": "low", "analyst": "low", "write": "max", "review": "low", "revise": "low"},
    }


def test_normalize_reasoning_config_drops_invalid_values():
    # Hand-edited JSON never raises: invalid default/seat levels fall back.
    normalized = normalize_reasoning_config({"default": "ultra", "per_role": {"write": "turbo", "bogus_seat": "high"}})
    assert normalized["default"] == DEFAULT_CLUSTER_REASONING["default"]
    assert normalized["per_role"]["write"] == DEFAULT_CLUSTER_REASONING["default"]
    assert "bogus_seat" not in normalized["per_role"]


def test_reasoning_level_for_per_role_override_wins():
    reasoning = {"default": "high", "per_role": {"orchestrator": "none"}}
    assert reasoning_level_for(reasoning, "orchestrator") == "none"
    assert reasoning_level_for(reasoning, "write") == "high"
    # Unknown seat (defensive): the default applies.
    assert reasoning_level_for(reasoning, "unknown") == "high"


@pytest.mark.asyncio
async def test_get_cluster_config_legacy_row_falls_back_to_default_reasoning():
    # A stored preference without the reasoning key still resolves the
    # defaults (shallow-merge fallback, no migration needed).
    uow = _uow({"mode": "cluster", "write_model": "openai/w"}, ["openai"], [("openai", "w")])
    config = await get_cluster_config(uow, "u1")
    assert config["reasoning"] == DEFAULT_CLUSTER_REASONING
