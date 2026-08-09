from .chat_completions import ChatCompletionsProvider


class MiniMaxProvider(ChatCompletionsProvider):
    provider_id = "minimax"
    default_base_url = "https://api.minimaxi.com/v1"
