from .chat_completions import ChatCompletionsProvider


class YiProvider(ChatCompletionsProvider):
    provider_id = "yi"
    default_base_url = "https://api.lingyiwanwu.com/v1"
