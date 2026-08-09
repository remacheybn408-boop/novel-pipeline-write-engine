from .chat_completions import ChatCompletionsProvider


class DeepSeekProvider(ChatCompletionsProvider):
    provider_id = "deepseek"
    default_base_url = "https://api.deepseek.com"
