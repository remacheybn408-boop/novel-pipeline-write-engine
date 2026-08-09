from .chat_completions import ChatCompletionsProvider


class SambaNovaProvider(ChatCompletionsProvider):
    provider_id = "sambanova"
    default_base_url = "https://api.sambanova.ai/v1"
