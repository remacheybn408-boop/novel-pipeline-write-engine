from proseforge.api.main import create_app


def test_application_registers_supported_provider_families():
    registry = create_app().state.provider_registry
    assert {"openai", "anthropic", "google", "deepseek", "kimi", "dashscope", "zhipu", "volcengine", "baidu", "tencent", "minimax", "xai", "mistral", "cohere", "ollama", "vllm"} <= set(registry.ids())
