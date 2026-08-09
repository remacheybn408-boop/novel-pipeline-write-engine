/**
 * Display metadata for the provider registry ids built into the backend
 * (see proseforge/providers/registry or factory). Unknown ids fall back to
 * the raw id so future providers render without a frontend update.
 */

export const PROVIDER_LABELS: Record<string, string> = {
  agnes: "Agnes AI",
  openai: "OpenAI",
  anthropic: "Anthropic",
  baichuan: "百川智能",
  baidu: "百度千帆",
  cohere: "Cohere",
  dashscope: "阿里百炼",
  deepseek: "深度求索",
  google: "Google",
  iflytek: "讯飞星火",
  kimi: "月之暗面 Kimi",
  minimax: "MiniMax",
  mistral: "Mistral",
  ollama: "Ollama",
  sensenova: "商汤日日新",
  stepfun: "阶跃星辰",
  tencent: "腾讯混元",
  vllm: "vLLM",
  volcengine: "火山引擎",
  xai: "xAI",
  yi: "零一万物",
  zhipu: "智谱",
};

export function providerLabel(id: string): string {
  return PROVIDER_LABELS[id] ?? id;
}

/**
 * Providers with an OpenAI-compatible POST {base_url}/embeddings endpoint —
 * the only shape the vector engine client speaks
 * (infrastructure/embeddings/client.py). Chat-only vendors (深度求索, Kimi,
 * Anthropic, …) have no embedding endpoint at all; listing them in the RAG
 * settings dropdown produced configs that could never work.
 */
export const EMBEDDING_CAPABLE_PROVIDERS: ReadonlySet<string> = new Set([
  "openai",
  "dashscope", // 阿里百炼 text-embedding-v4
  "zhipu", // embedding-3
  "volcengine", // doubao-embedding
  "siliconflow", // BAAI/bge-m3 等
  "mistral", // mistral-embed
  "deepinfra",
  "novita",
  "together",
  "fireworks",
  "openrouter",
  "google", // Gemini OpenAI-compat 含 embeddings
  "vllm", // 自托管 OpenAI 兼容
  "ollama", // 自托管 OpenAI 兼容
]);

export function providerSupportsEmbedding(id: string): boolean {
  return EMBEDDING_CAPABLE_PROVIDERS.has(id);
}

/** Default Base URL hints shown as input placeholders per provider. */
export const PROVIDER_BASE_URL_HINTS: Record<string, string> = {
  openai: "https://api.openai.com/v1",
  volcengine: "https://ark.cn-beijing.volces.com/api/v3",
  deepseek: "https://api.deepseek.com/v1",
  dashscope: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  zhipu: "https://open.bigmodel.cn/api/paas/v4",
  kimi: "https://api.moonshot.cn/v1",
  // No /v1 suffix: the backend appends /api/tags to the base URL directly.
  ollama: "http://localhost:11434",
  stepfun: "https://api.stepfun.com/v1",
  yi: "https://api.lingyiwanwu.com/v1",
  baichuan: "https://api.baichuan-ai.com/v1",
  iflytek: "https://spark-api-open.xf-yun.com/v1",
  sensenova: "https://token.sensenova.cn/v1",
  agnes: "https://apihub.agnes-ai.com/v1",
};
