from .chat_completions import ChatCompletionsProvider


class KimiProvider(ChatCompletionsProvider):
    provider_id = "kimi"
    default_base_url = "https://api.moonshot.cn/v1"
