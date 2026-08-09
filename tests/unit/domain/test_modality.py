"""Modality filter rules (domain/model/modality.py)."""

from __future__ import annotations

import pytest

from proseforge.domain.model.modality import (
    is_text_or_omni_model,
    supports_vision_by_name,
)

EXCLUDED = (
    "agnes-image-2.0-flash",  # image generation seen in the production catalog
    "agnes-video-v2.0",  # video generation seen in the production catalog
    "whisper-1",  # OpenAI ASR
    "tts-1-hd",  # OpenAI TTS
    "dall-e-3",  # OpenAI image generation
    "text-embedding-3-large",  # OpenAI embedding
    "doubao-embedding",  # Volcengine embedding
    "bge-m3",  # BGE embedding
    "kolors",  # Kling image generation
    "CosyVoice2",  # TTS (case-insensitive match)
    "seedance-1.0",  # Volcengine video generation
    "seedream-3.0",  # Volcengine image generation
    "cogview-3",  # Zhipu image generation
    "rerank-v1",  # reranker
)

KEPT = (
    "deepseek-chat",
    "gpt-4o",
    "doubao-pro-32k",
    "doubao-vision-pro-32k",  # "vision" must NOT trigger exclusion
    "qwen-vl-max",
    "glm-4v",
    "claude-sonnet-4",
    "gemini-2.5-pro",
    "qwen-omni-turbo",  # "omni" must NOT trigger exclusion
    "yi-vision",
)

VISION = ("gpt-4o", "doubao-vision-pro-32k", "qwen-vl-max", "glm-4v", "claude-sonnet-4", "gemini-2.5-pro", "qwen-omni-turbo", "yi-vision")
TEXT_ONLY = ("deepseek-chat", "doubao-pro-32k")


@pytest.mark.parametrize("model_id", EXCLUDED)
def test_non_text_models_are_excluded(model_id: str):
    assert is_text_or_omni_model(model_id) is False


@pytest.mark.parametrize("model_id", KEPT)
def test_text_and_omni_models_are_kept(model_id: str):
    assert is_text_or_omni_model(model_id) is True


@pytest.mark.parametrize("model_id", VISION)
def test_omni_models_detect_vision(model_id: str):
    assert supports_vision_by_name(model_id) is True


@pytest.mark.parametrize("model_id", TEXT_ONLY)
def test_text_models_do_not_detect_vision(model_id: str):
    assert supports_vision_by_name(model_id) is False
