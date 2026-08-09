from .chat_completions import ChatCompletionsProvider


class FireworksProvider(ChatCompletionsProvider):
    provider_id = "fireworks"
    default_base_url = "https://api.fireworks.ai/inference/v1"
