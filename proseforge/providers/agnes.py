from .chat_completions import ChatCompletionsProvider


class AgnesProvider(ChatCompletionsProvider):
    provider_id = "agnes"
    default_base_url = "https://apihub.agnes-ai.com/v1"
