from .chat_completions import ChatCompletionsProvider


class BaiduProvider(ChatCompletionsProvider):
    provider_id = "baidu"
    default_base_url = "https://qianfan.baidubce.com/v2"
