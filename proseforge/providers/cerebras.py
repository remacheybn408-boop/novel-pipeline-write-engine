from .chat_completions import ChatCompletionsProvider


class CerebrasProvider(ChatCompletionsProvider):
    provider_id = "cerebras"
    default_base_url = "https://api.cerebras.ai/v1"
