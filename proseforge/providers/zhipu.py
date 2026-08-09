from .chat_completions import ChatCompletionsProvider


class ZhipuProvider(ChatCompletionsProvider):
    provider_id = "zhipu"
    default_base_url = "https://open.bigmodel.cn/api/paas/v4"
