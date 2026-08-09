/**
 * The Ollama base-URL hint must NOT carry a /v1 suffix: the backend Ollama
 * adapter appends /api/tags and /api/chat to the base URL directly (unlike
 * the vLLM adapter, which strips /v1), so a hinted /v1 URL would 404.
 */
import { describe, expect, it } from "vitest";
import { PROVIDER_BASE_URL_HINTS, providerSupportsEmbedding } from "./providers";

describe("PROVIDER_BASE_URL_HINTS", () => {
  it("hints the bare Ollama origin, without a /v1 suffix", () => {
    expect(PROVIDER_BASE_URL_HINTS.ollama).toBe("http://localhost:11434");
    expect(PROVIDER_BASE_URL_HINTS.ollama).not.toContain("/v1");
  });
});

// The RAG settings dropdown must only offer vendors with an
// OpenAI-compatible /embeddings endpoint — picking a chat-only vendor
// produced a vector engine config that could never work.
describe("providerSupportsEmbedding", () => {
  it("accepts vendors with an embeddings endpoint", () => {
    for (const id of ["openai", "dashscope", "zhipu", "volcengine", "siliconflow"]) {
      expect(providerSupportsEmbedding(id)).toBe(true);
    }
  });

  it("rejects chat-only vendors", () => {
    for (const id of ["deepseek", "kimi", "anthropic", "perplexity"]) {
      expect(providerSupportsEmbedding(id)).toBe(false);
    }
  });

  it("rejects unknown provider ids", () => {
    expect(providerSupportsEmbedding("some-future-vendor")).toBe(false);
  });
});
