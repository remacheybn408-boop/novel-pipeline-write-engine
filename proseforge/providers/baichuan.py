from .chat_completions import ChatCompletionsProvider


class BaichuanProvider(ChatCompletionsProvider):
    provider_id = "baichuan"
    default_base_url = "https://api.baichuan-ai.com/v1"
