from .chat_completions import ChatCompletionsProvider


class OpenRouterProvider(ChatCompletionsProvider):
    provider_id = "openrouter"
    default_base_url = "https://openrouter.ai/api/v1"
