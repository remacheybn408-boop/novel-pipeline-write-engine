
from proseforge.providers.deepseek import DeepSeekProvider
from proseforge.providers.ollama import OllamaProvider


def test_provider_ids_are_explicit_and_local_defaults_are_container_names():
    assert DeepSeekProvider.provider_id == "deepseek"
    assert OllamaProvider().base_url == "http://ollama:11434"
