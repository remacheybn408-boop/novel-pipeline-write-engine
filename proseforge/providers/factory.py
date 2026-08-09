from __future__ import annotations

from collections.abc import Callable

from proseforge.domain.ports.model_provider import ModelProvider
from proseforge.providers.agnes import AgnesProvider
from proseforge.providers.anthropic import AnthropicProvider
from proseforge.providers.baichuan import BaichuanProvider
from proseforge.providers.baidu import BaiduProvider
from proseforge.providers.cerebras import CerebrasProvider
from proseforge.providers.cohere import CohereProvider
from proseforge.providers.dashscope import DashScopeProvider
from proseforge.providers.deepinfra import DeepInfraProvider
from proseforge.providers.deepseek import DeepSeekProvider
from proseforge.providers.fireworks import FireworksProvider
from proseforge.providers.google import GoogleProvider
from proseforge.providers.groq import GroqProvider
from proseforge.providers.iflytek import IFlytekProvider
from proseforge.providers.kimi import KimiProvider
from proseforge.providers.minimax import MiniMaxProvider
from proseforge.providers.mistral import MistralProvider
from proseforge.providers.novita import NovitaProvider
from proseforge.providers.ollama import OllamaProvider
from proseforge.providers.openai import OpenAIProvider
from proseforge.providers.openrouter import OpenRouterProvider
from proseforge.providers.perplexity import PerplexityProvider
from proseforge.providers.sambanova import SambaNovaProvider
from proseforge.providers.sensenova import SenseNovaProvider
from proseforge.providers.siliconflow import SiliconFlowProvider
from proseforge.providers.stepfun import StepFunProvider
from proseforge.providers.tencent import TencentProvider
from proseforge.providers.together import TogetherProvider
from proseforge.providers.vllm import VLLMProvider
from proseforge.providers.volcengine import VolcEngineProvider
from proseforge.providers.xai import XAIProvider
from proseforge.providers.yi import YiProvider
from proseforge.providers.zhipu import ZhipuProvider

PROVIDER_FACTORIES: dict[str, Callable[..., ModelProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
    "baidu": BaiduProvider,
    "cohere": CohereProvider,
    "dashscope": DashScopeProvider,
    "deepseek": DeepSeekProvider,
    "kimi": KimiProvider,
    "minimax": MiniMaxProvider,
    "mistral": MistralProvider,
    "ollama": OllamaProvider,
    "tencent": TencentProvider,
    "vllm": VLLMProvider,
    "volcengine": VolcEngineProvider,
    "xai": XAIProvider,
    "zhipu": ZhipuProvider,
    "stepfun": StepFunProvider,
    "yi": YiProvider,
    "baichuan": BaichuanProvider,
    "iflytek": IFlytekProvider,
    "sensenova": SenseNovaProvider,
    "siliconflow": SiliconFlowProvider,
    "openrouter": OpenRouterProvider,
    "groq": GroqProvider,
    "together": TogetherProvider,
    "fireworks": FireworksProvider,
    "perplexity": PerplexityProvider,
    "cerebras": CerebrasProvider,
    "sambanova": SambaNovaProvider,
    "deepinfra": DeepInfraProvider,
    "novita": NovitaProvider,
    "agnes": AgnesProvider,
}


def build_provider(provider_id: str, api_key: str, base_url: str | None = None) -> ModelProvider:
    factory = PROVIDER_FACTORIES.get(provider_id)
    if factory is None:
        raise KeyError(provider_id)
    if provider_id in {"ollama", "vllm"}:
        return factory(api_key, base_url=base_url) if base_url else factory(api_key)
    return factory(api_key, base_url=base_url) if base_url else factory(api_key)
