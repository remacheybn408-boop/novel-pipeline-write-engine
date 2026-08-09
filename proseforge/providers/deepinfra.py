from .chat_completions import ChatCompletionsProvider


class DeepInfraProvider(ChatCompletionsProvider):
    provider_id = "deepinfra"
    default_base_url = "https://api.deepinfra.com/v1/openai"
