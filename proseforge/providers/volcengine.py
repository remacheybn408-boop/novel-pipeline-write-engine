from .chat_completions import ChatCompletionsProvider


class VolcEngineProvider(ChatCompletionsProvider):
    provider_id = "volcengine"
    default_base_url = "https://ark.cn-beijing.volces.com/api/v3"
