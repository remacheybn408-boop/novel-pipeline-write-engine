from .chat_completions import ChatCompletionsProvider


class SiliconFlowProvider(ChatCompletionsProvider):
    provider_id = "siliconflow"
    default_base_url = "https://api.siliconflow.cn/v1"
