import pytest

from proseforge.application.models.reasoning_policy import resolve_reasoning
from proseforge.domain.model.capabilities import ModelCapabilities


def test_auto_does_not_claim_deep_reasoning():
    caps = ModelCapabilities(8192, 1024, False, None, False, False, "catalog")
    assert resolve_reasoning("auto", caps)["parameter"] is None
    with pytest.raises(ValueError, match="unsupported"):
        resolve_reasoning("max", caps)


def test_reasoning_off_parameter_uses_none_profile_payload():
    from proseforge.application.models.reasoning_policy import reasoning_off_parameter
    from proseforge.domain.model.known_reasoning import ReasoningProfile

    profile = ReasoningProfile("reasoning_effort", {"none": {"thinking": {"type": "disabled"}}, "high": {"reasoning_effort": "high"}})
    caps = ModelCapabilities(700000, 64000, True, None, False, False, "catalog", profile)
    assert reasoning_off_parameter(caps) == {"thinking": {"type": "disabled"}}


def test_reasoning_off_parameter_none_without_none_payload():
    from proseforge.application.models.reasoning_policy import reasoning_off_parameter
    from proseforge.domain.model.known_reasoning import ReasoningProfile

    # Profile without a "none" level: nothing honest to send.
    profile = ReasoningProfile("reasoning_effort", {"high": {"reasoning_effort": "high"}})
    caps = ModelCapabilities(700000, 64000, True, None, False, False, "catalog", profile)
    assert reasoning_off_parameter(caps) is None
    # No profile at all (model without verified reasoning mapping).
    plain = ModelCapabilities(8192, 1024, False, None, False, False, "catalog")
    assert reasoning_off_parameter(plain) is None


def test_openai_gateway_deepseek_v4_none_tier_resolves():
    # OpenAI-compatible gateways (opencode zen/go) serve deepseek-v4 family
    # under provider "openai"; reasoning_off must resolve through lookup.
    from proseforge.application.models.reasoning_policy import reasoning_off_parameter
    from proseforge.domain.model.known_reasoning import lookup_reasoning

    profile = lookup_reasoning("openai", "deepseek-v4-flash")
    assert profile is not None
    assert "none" in profile.level_payloads
    caps = ModelCapabilities(700000, 64000, True, None, False, False, "catalog", profile)
    assert reasoning_off_parameter(caps) == {"reasoning_effort": "none"}
    # Real OpenAI models must not be captured by the gateway rule.
    assert lookup_reasoning("openai", "gpt-5.6") is not None
    assert "deepseek" not in str(lookup_reasoning("openai", "gpt-5.6").level_payloads)


def test_openai_gateway_profiles_from_2026_08_08_research():
    # Scoped gateway entries (opencode zen/go) added from the 2026-08-08 swarm
    # research (tmp/model_research_2026-08-08.json).
    from proseforge.domain.model.known_reasoning import lookup_reasoning

    flash = lookup_reasoning("openai", "deepseek-v4-flash")
    assert set(flash.level_payloads) == {"none", "low", "high", "max"}
    assert flash.clamp == {"medium": "high", "xhigh": "high"}
    pro = lookup_reasoning("openai", "deepseek-v4-pro")
    assert set(pro.level_payloads) == {"none", "high", "max"}
    assert pro.clamp == {"low": "high", "medium": "high", "xhigh": "max"}
    # glm-5.2 (effort + thinking enable) must beat the glm-5 toggle prefix.
    glm52 = lookup_reasoning("openai", "glm-5.2")
    assert set(glm52.level_payloads) == {"none", "high", "max"}
    glm5 = lookup_reasoning("openai", "glm-5.1")
    assert glm5.level_payloads["none"] == {"thinking": {"type": "disabled"}}
    assert glm5.level_payloads["high"] == {"thinking": {"type": "enabled"}}
    assert lookup_reasoning("openai", "kimi-k2.6").level_payloads["high"] == {"thinking": {"type": "enabled"}}
    k3 = lookup_reasoning("openai", "kimi-k3")
    assert set(k3.level_payloads) == {"low", "high", "max"}
    qwen = lookup_reasoning("openai", "qwen3.6-plus")
    assert qwen.parameter == "thinking_budget"
    assert qwen.level_payloads["none"] == {"enable_thinking": False}
    mimo_pro = lookup_reasoning("openai", "mimo-v2.5-pro")
    assert set(mimo_pro.level_payloads) == {"low", "medium", "high"}
    mimo = lookup_reasoning("openai", "mimo-v2-omni")
    assert set(mimo.level_payloads) == {"none", "high"}
    hy3 = lookup_reasoning("openai", "hy3-preview")
    assert set(hy3.level_payloads) == {"none", "low", "high"}
    assert hy3.clamp == {"medium": "high"}
    grok = lookup_reasoning("openai", "grok-4.5")
    assert set(grok.level_payloads) == {"low", "medium", "high"}
    # Force-thinking / unverified models stay auto-only, deliberately unlisted.
    assert lookup_reasoning("openai", "kimi-k2.7-code") is None
    assert lookup_reasoning("openai", "minimax-m2.5") is None
    assert lookup_reasoning("openai", "qwen3.7-max") is None


# ---------------------------------------------------------------------------
# resolve_task_reasoning — elastic per-task matrix (cluster runs)
# ---------------------------------------------------------------------------

from proseforge.application.models.reasoning_policy import (
    is_prose_task,
    resolve_task_reasoning,
)
from proseforge.domain.model.known_reasoning import lookup_reasoning


def _caps(profile=None, max_output=64000, supports=None):
    return ModelCapabilities(
        700000,
        max_output,
        profile is not None if supports is None else supports,
        None,
        False,
        False,
        "catalog",
        profile,
    )


def test_is_prose_task_covers_scene_drafts_and_rewrite():
    assert is_prose_task("scene_a") and is_prose_task("scene_d")
    assert is_prose_task("rewrite")
    for key in ("planner", "character", "select", "merge", "recheck", "promise_register", "review_style", "classify", "analyze_structure"):
        assert not is_prose_task(key)


def test_prose_task_keeps_seat_level_and_boosts_max_output():
    # deepseek-v4-flash serves max natively: no step-down, reserve = base.
    caps = _caps(lookup_reasoning("openai", "deepseek-v4-flash"), max_output=384000)
    result = resolve_task_reasoning("scene_a", "max", caps, 30000)
    assert result["level"] == "max"
    assert result["provider_parameter"] == {"reasoning_effort": "max"}
    assert result["max_output"] == 60000
    assert result["warnings"] == []


def test_prose_task_max_output_reserve_capped_at_model_ceiling():
    caps = _caps(lookup_reasoning("openai", "deepseek-v4-flash"), max_output=40000)
    result = resolve_task_reasoning("rewrite", "max", caps, 30000)
    # base + reserve (30K) would exceed the model's own 40K ceiling.
    assert result["max_output"] == 40000


def test_prose_task_steps_down_to_nearest_supported_tier():
    # mimo-v2-omni serves {none, high}: max steps down to high with a warning.
    caps = _caps(lookup_reasoning("openai", "mimo-v2-omni"))
    result = resolve_task_reasoning("scene_b", "max", caps, 16000)
    assert result["level"] == "high"
    assert result["provider_parameter"] == {"reasoning_effort": "high"}
    assert result["max_output"] == 24000  # high reserve = 50%
    assert any("stepped down" in warning for warning in result["warnings"])


def test_prose_task_profile_clamp_warns_without_stepping_down():
    # deepseek-v4-pro clamps xhigh->max via its profile mapping: the clamp
    # warning surfaces and the payload is the clamped tier's.
    caps = _caps(lookup_reasoning("openai", "deepseek-v4-pro"))
    result = resolve_task_reasoning("rewrite", "xhigh", caps, 8000)
    assert result["level"] == "xhigh"
    assert result["provider_parameter"] == {"reasoning_effort": "max"}
    assert any("clamped" in warning for warning in result["warnings"])


def test_prose_task_unsupported_everywhere_falls_back_to_auto():
    plain = _caps(None, supports=False)
    result = resolve_task_reasoning("scene_a", "high", plain, 8000)
    assert result["level"] == "auto"
    assert result["provider_parameter"] is None
    assert result["max_output"] == 8000  # no reserve without a payload
    assert any("falling back to auto" in warning for warning in result["warnings"])


def test_structured_task_prefers_none_payload_over_low():
    # The existing reasoning_off protection: profiles with a none tier keep
    # thinking fully off regardless of the seat level.
    caps = _caps(lookup_reasoning("openai", "deepseek-v4-flash"))
    result = resolve_task_reasoning("planner", "max", caps, 8000)
    assert result["level"] == "none"
    assert result["provider_parameter"] == {"reasoning_effort": "none"}
    assert result["max_output"] == 8000


def test_structured_task_without_none_payload_uses_low():
    # kimi-k3 serves {low, high, max} (no none): the smallest tier applies.
    caps = _caps(lookup_reasoning("openai", "kimi-k3"))
    result = resolve_task_reasoning("promise_register", "high", caps, 4000)
    assert result["level"] == "low"
    assert result["provider_parameter"] == {"reasoning_effort": "low"}
    assert result["max_output"] == 4000  # structured tasks never get a reserve


def test_structured_task_on_plain_model_falls_back_to_auto():
    plain = _caps(None, supports=False)
    result = resolve_task_reasoning("select", "high", plain, 8000)
    assert result["level"] == "auto"
    assert result["provider_parameter"] is None


def test_classify_task_follows_the_structured_rule():
    # Orchestrator intent classification: tiny output, thinking off/low.
    caps = _caps(lookup_reasoning("openai", "deepseek-v4-flash"))
    assert resolve_task_reasoning("classify", "low", caps, 16)["level"] == "none"
    kimi = _caps(lookup_reasoning("openai", "kimi-k3"))
    assert resolve_task_reasoning("classify", "low", kimi, 16)["provider_parameter"] == {"reasoning_effort": "low"}
    # Budget-style low payload cannot materialize under the 1024 floor: no
    # payload is sent rather than one the provider would reject.
    from proseforge.domain.model.known_reasoning import ReasoningProfile

    budget_caps = _caps(ReasoningProfile("thinking", {"low": {"thinking": {"type": "enabled", "budget_ratio": 0.25}}}), max_output=64000)
    result = resolve_task_reasoning("classify", "low", budget_caps, 16)
    assert result["provider_parameter"] is None


def test_prose_task_explicit_auto_and_none_seat_levels():
    caps = _caps(lookup_reasoning("openai", "deepseek-v4-flash"))
    auto = resolve_task_reasoning("scene_a", "auto", caps, 30000)
    assert auto["level"] == "auto" and auto["provider_parameter"] is None and auto["max_output"] == 30000
    none = resolve_task_reasoning("scene_a", "none", caps, 30000)
    assert none["level"] == "none" and none["provider_parameter"] == {"reasoning_effort": "none"}
