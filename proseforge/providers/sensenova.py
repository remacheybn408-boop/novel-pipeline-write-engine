from .chat_completions import ChatCompletionsProvider


class SenseNovaProvider(ChatCompletionsProvider):
    provider_id = "sensenova"
    default_base_url = "https://token.sensenova.cn/v1"
