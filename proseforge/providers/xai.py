from .chat_completions import ChatCompletionsProvider


class XAIProvider(ChatCompletionsProvider):
    provider_id = "xai"
    default_base_url = "https://api.x.ai/v1"
