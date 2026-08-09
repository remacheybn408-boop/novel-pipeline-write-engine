from .chat_completions import ChatCompletionsProvider


class StepFunProvider(ChatCompletionsProvider):
    provider_id = "stepfun"
    default_base_url = "https://api.stepfun.com/v1"
