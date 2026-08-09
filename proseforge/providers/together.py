from .chat_completions import ChatCompletionsProvider


class TogetherProvider(ChatCompletionsProvider):
    provider_id = "together"
    default_base_url = "https://api.together.xyz/v1"
