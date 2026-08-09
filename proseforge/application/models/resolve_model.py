"""模型解析（V2-004 接入 send/generate 路径）。

优先级：user override（请求显式选择的 provider/model/level）→ provider
response（sync 进 catalog 的条目）→ checked catalog → conservative
fallback。catalog 命中即事实；未命中回落保守能力且 ``source="fallback"``
显式记录，绝不静默。
"""

from __future__ import annotations

from proseforge.application.models.reasoning_policy import resolve_reasoning
from proseforge.domain.model.capabilities import (
    ModelCapabilities,
    capabilities_from_model,
)

# 未知模型 → 保守 fallback（send/generate 路径共用同一份定义）。
# max_output_tokens 与 capabilities_from_model 同兜底：8192（主流模型输出上限
# 均 ≥8K 的安全值；1024 会被思考强度烧光导致空回复）。
FALLBACK_CAPABILITIES = ModelCapabilities(8192, 8192, False, None, False, False, "fallback")


def resolve_capabilities(model) -> ModelCapabilities:
    """catalog 行 → ModelCapabilities；未知模型 → 保守 fallback。"""
    return capabilities_from_model(model) if model is not None else FALLBACK_CAPABILITIES


def resolve_model(model, reasoning: str = "auto") -> dict[str, object]:
    capabilities = resolve_capabilities(model)
    policy = resolve_reasoning(reasoning, capabilities)
    return {
        "provider": model.provider if model is not None else "unknown",
        "model_id": model.model_id if model is not None else "unknown",
        "capabilities": capabilities,
        "reasoning": policy,
    }
