from .chat_completions import ChatCompletionsProvider


class GroqProvider(ChatCompletionsProvider):
    provider_id = "groq"
    default_base_url = "https://api.groq.com/openai/v1"
