from proseforge.providers.usage import normalize_provider_usage


def test_google_usage_metadata_maps_to_common_usage_delta():
    delta = normalize_provider_usage("google", {"usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 3, "totalTokenCount": 7}})

    assert delta.input_tokens == 4
    assert delta.output_tokens == 3
    assert delta.total_tokens == 7
    assert delta.source == "provider"


def test_missing_usage_is_explicitly_estimated():
    delta = normalize_provider_usage("ollama", {}, estimated=True)

    assert delta.source == "estimated"
    assert delta.total_tokens == 0


def test_provider_response_without_any_usage_is_missing_not_provider():
    delta = normalize_provider_usage("openai", {})

    assert delta.source == "missing"
    assert delta.total_tokens == 0


def test_empty_usage_dict_is_missing_not_provider():
    delta = normalize_provider_usage("openai", {"usage": {}})

    assert delta.source == "missing"


def test_ollama_without_eval_counters_is_missing():
    delta = normalize_provider_usage("ollama", {})

    assert delta.source == "missing"


def test_ollama_eval_counters_are_provider_usage():
    delta = normalize_provider_usage("ollama", {"prompt_eval_count": 5, "eval_count": 7})

    assert delta.source == "provider"
    assert delta.input_tokens == 5
    assert delta.output_tokens == 7


def test_google_without_usage_metadata_is_missing():
    delta = normalize_provider_usage("google", {"candidates": []})

    assert delta.source == "missing"


def test_estimated_stays_estimated_even_without_usage():
    delta = normalize_provider_usage("ollama", {}, estimated=True)

    assert delta.source == "estimated"
