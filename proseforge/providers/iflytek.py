from .chat_completions import ChatCompletionsProvider


class IFlytekProvider(ChatCompletionsProvider):
    provider_id = "iflytek"
    default_base_url = "https://spark-api-open.xf-yun.com/v1"
