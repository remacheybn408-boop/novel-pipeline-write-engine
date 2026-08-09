from .chat_completions import ChatCompletionsProvider


class TencentProvider(ChatCompletionsProvider):
    provider_id = "tencent"
    default_base_url = "https://api.hunyuan.cloud.tencent.com/v1"
