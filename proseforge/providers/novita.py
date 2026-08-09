from .chat_completions import ChatCompletionsProvider


class NovitaProvider(ChatCompletionsProvider):
    provider_id = "novita"
    default_base_url = "https://api.novita.ai/openai/v1"
