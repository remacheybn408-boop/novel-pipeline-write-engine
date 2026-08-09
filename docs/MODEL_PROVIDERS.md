# Model providers

Provider credentials are entered in Settings and encrypted by the API with AES-256-GCM. The browser never receives the original key. `base_url` is optional for hosted vendors; local endpoints require an explicit allow-list entry.

## Supported adapters

| Provider | Protocol | Default endpoint |
| --- | --- | --- |
| OpenAI | Responses API | `https://api.openai.com/v1` |
| Anthropic | Messages API | `https://api.anthropic.com/v1` |
| Google | Gemini API | `https://generativelanguage.googleapis.com/v1beta` |
| xAI, DeepSeek, Kimi, DashScope, Zhipu, VolcEngine, Baidu, Tencent, MiniMax, Mistral, Cohere | Vendor OpenAI-compatible Chat Completions contract | vendor default |
| Ollama | Native `/api/tags` and `/api/chat` | `http://ollama:11434` |
| vLLM | OpenAI-compatible `/v1` | `http://vllm:8000` |

Provider model IDs are not hardcoded. After saving credentials, call `POST /api/v1/providers/{provider_id}/sync-models` from the Settings UI or API. The server calls the provider's model listing endpoint and persists the returned IDs in PostgreSQL. Unknown future IDs are accepted without a code change.

## Local endpoints

Local URLs are rejected by default to prevent SSRF. Configure `PROSEFORGE_ALLOWED_LOCAL_PROVIDER_HOSTS` and enable the local endpoint option only for hosts you operate. Do not expose a Docker-internal service or a cloud metadata address through a user-controlled URL.

## Production secrets

Set a random `PROSEFORGE_MASTER_KEY` and a strong `PROSEFORGE_JWT_SECRET`. Never commit either value or place a full API key in frontend local storage, logs, screenshots, or bug reports.
