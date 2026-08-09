from .chat_completions import ChatCompletionsProvider


class DashScopeProvider(ChatCompletionsProvider):
    provider_id = "dashscope"
    default_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
