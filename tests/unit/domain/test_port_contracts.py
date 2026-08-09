from proseforge.domain.ports.model_provider import ModelProvider


def test_model_provider_defines_stream_and_list_models() -> None:
    assert hasattr(ModelProvider, "stream")
    assert hasattr(ModelProvider, "list_models")
