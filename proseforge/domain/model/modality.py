"""Model modality filters for the catalog intake path.

This is intake hygiene, NOT a security boundary: provider /models endpoints
return every product the vendor sells (chat, image generation, video, TTS,
ASR, embeddings, rerankers...), and the catalog exists to feed text chat
generation. Anything that is not a text or omni (text+vision) chat model is
noise for every downstream consumer, so it is filtered once at the upsert
choke point rather than at each read site.
"""

from __future__ import annotations

# Non-text modalities, matched as lowercase substrings against model_id.
# Deliberately absent: "vision" (doubao-vision / glm-4v / qwen-vl are omni
# chat models we must KEEP) and "omni" (qwen-omni is an omni chat model).
_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "embed",  # embedding models (text-embedding-3, doubao-embedding, ...)
    "rerank",  # reranker endpoints
    "tts",  # text-to-speech
    "whisper",  # OpenAI ASR
    "dall",  # DALL-E image generation
    "gpt-image",  # OpenAI gpt-image image generation
    "image",  # image generation (agnes-image-*, ...); chat models never carry it
    "video",  # video generation (agnes-video, ...); also covers cogvideo
    "audio",  # audio models; also covers fish-audio
    "moderation",  # moderation endpoints
    "seedance",  # Volcengine video generation
    "seedream",  # Volcengine image generation
    "kolors",  # Kling image generation
    "cosyvoice",  # TTS
    "fish-audio",  # TTS
    "bge-",  # BGE embedding / reranker series
    "cogview",  # Zhipu image generation
    "cogvideo",  # Zhipu video generation (redundant with "video", kept explicit)
    "wanx",  # Alibaba Wanx image/video generation
    "flux",  # Flux image generation
    "stable-diffusion",  # Stable Diffusion image generation
    "sd3",  # Stable Diffusion 3
    "sdxl",  # SDXL
    "midjourney",  # Midjourney image generation
    "clip",  # CLIP encoders (not chat models)
    "blip",  # BLIP captioning models
    "vits",  # VITS TTS
    "speech",  # speech synthesis/recognition
    "voice",  # voice models (TTS/voice-clone)
    "asr",  # speech recognition
)

# Omni (text+vision) chat model families, lowercase substrings. Used to stamp
# capabilities["vision"] on intake; vendors do not report this flag.
_VISION_PATTERNS: tuple[str, ...] = (
    "gpt-4o",  # OpenAI omni
    "gpt-4-vision",  # OpenAI first vision generation
    "claude-3",  # Anthropic vision-capable generation
    "claude-4",  # Anthropic vision-capable generation
    "sonnet",  # Anthropic family ids (claude-sonnet-4, claude-3-5-sonnet)
    "opus",  # Anthropic family ids (claude-opus-4.x)
    "haiku",  # Anthropic family ids (claude-haiku-4.x)
    "gemini",  # Google multimodal
    "qwen-vl",  # Alibaba vision-language
    "doubao-vision",  # Volcengine vision
    "glm-4v",  # Zhipu vision
    "llava",  # open vision-language
    "pixtral",  # Mistral vision
    "yi-vision",  # 01.AI vision
    "step-1v",  # StepFun vision
    "internvl",  # InternVL vision-language
    "-vl",  # generic vision-language suffix (qwen2.5-vl, ...)
    "vision",  # generic vision marker (checked AFTER exclusion, so safe)
    "omni",  # omni-model marker (qwen-omni, ...)
)


def is_text_or_omni_model(model_id: str) -> bool:
    """True when the model can serve text chat (pure text or omni)."""
    lowered = model_id.lower()
    return not any(pattern in lowered for pattern in _EXCLUDE_PATTERNS)


def supports_vision_by_name(model_id: str) -> bool:
    """Best-effort omni detection from the model id (vendors report nothing)."""
    lowered = model_id.lower()
    return any(pattern in lowered for pattern in _VISION_PATTERNS)


def exclusion_regex() -> str:
    """Single alternation matching every excluded pattern (for SQL/migrations)."""
    return "|".join(_EXCLUDE_PATTERNS)
