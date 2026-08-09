from .chat_completions import ChatCompletionsProvider


class PerplexityProvider(ChatCompletionsProvider):
    provider_id = "perplexity"
    default_base_url = "https://api.perplexity.ai"
