"""模型 context window 解析（V2-004）。

catalog 是窗口大小的唯一事实来源；未知模型回落到保守下限 8192，且
source 记为 "fallback"——回落永远显式记录，绝不静默。调用方拿到的是
``{"context_window", "source"}`` 字典，方便直接落快照/事件。
"""

from __future__ import annotations

from proseforge.domain.model.capabilities import capabilities_from_model

CONSERVATIVE_FLOOR = 8192
FALLBACK_MAX_OUTPUT_TOKENS = 1024


def resolve_context_window(model_snapshot: dict | None, conservative_floor: int = CONSERVATIVE_FLOOR) -> dict[str, object]:
    """从 model_snapshot 解析可用 context window。

    快照携带合法的 ``context_window`` 时采用它（source 沿用快照来源）；
    缺失/非法时返回 conservative_floor 且 ``source="fallback"``。
    """
    if model_snapshot:
        try:
            candidate = int(model_snapshot.get("context_window") or 0)
        except (TypeError, ValueError):
            candidate = 0
        if candidate > 0:
            return {"context_window": candidate, "source": str(model_snapshot.get("source") or "catalog")}
    return {"context_window": conservative_floor, "source": "fallback"}


async def catalog_model_snapshot(uow, provider: str, model_id: str) -> dict[str, object]:
    """从 catalog 构建 model_snapshot；未知模型 → 保守 fallback（source 记录在案）。"""
    model = await uow.model_catalog.get(provider, model_id)
    if model is None:
        return {
            "provider": provider,
            "model": model_id,
            "context_window": CONSERVATIVE_FLOOR,
            "max_output_tokens": FALLBACK_MAX_OUTPUT_TOKENS,
            "source": "fallback",
        }
    capabilities = capabilities_from_model(model)
    return {
        "provider": provider,
        "model": model_id,
        "context_window": capabilities.context_window,
        "max_output_tokens": capabilities.max_output_tokens,
        "source": capabilities.source,
    }


async def default_catalog_snapshot(uow) -> dict[str, object] | None:
    """catalog 默认窗口：全部可用条目中的最小 context window。

    调用方未指定模型时，最小窗口是对任何可用模型都安全的保守界；
    catalog 为空返回 None（由 resolve_context_window 落到 floor）。
    """
    models = await uow.model_catalog.list(None, None, True)
    windows = [capabilities_from_model(model).context_window for model in models]
    if not windows:
        return None
    return {
        "provider": "",
        "model": "",
        "context_window": min(windows),
        "max_output_tokens": 0,
        "source": "catalog_default",
    }
