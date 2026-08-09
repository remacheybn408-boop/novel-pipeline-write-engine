from .chat_completions import ChatCompletionsProvider


class MistralProvider(ChatCompletionsProvider):
    provider_id = "mistral"
    default_base_url = "https://api.mistral.ai/v1"
