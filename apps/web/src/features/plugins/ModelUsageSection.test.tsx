// @vitest-environment jsdom
/**
 * ModelUsageSection: renders per-model usage rows (with display_name mapping
 * and model_id fallback) and an empty state when the window has no calls.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { ModelUsageSection } = await import("./PluginsPage");

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

function mockApi(usagePayload: unknown, modelsPayload: unknown[] = []) {
  const spy = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/v1/usage/by-model")) return jsonResponse(usagePayload);
    if (url.startsWith("/api/v1/models")) return jsonResponse(modelsPayload);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ModelUsageSection />
    </QueryClientProvider>,
  );
}

const USAGE_PAYLOAD = {
  days: 7,
  rows: [
    {
      provider: "openai",
      model_id: "gpt-4o",
      calls: 2,
      input_tokens: 300,
      output_tokens: 130,
      cached_input_tokens: 20,
      reasoning_tokens: 10,
      total_tokens: 430,
      cost_usd: 0.03,
      avg_latency_ms: 200.0,
      last_used_at: new Date().toISOString(),
    },
    {
      provider: "deepseek",
      model_id: "deepseek-chat",
      calls: 1,
      input_tokens: 500,
      output_tokens: 100,
      cached_input_tokens: 0,
      reasoning_tokens: 0,
      total_tokens: 600,
      cost_usd: null,
      avg_latency_ms: null,
      last_used_at: new Date().toISOString(),
    },
  ],
  totals: {
    calls: 3,
    input_tokens: 800,
    output_tokens: 230,
    cached_input_tokens: 20,
    reasoning_tokens: 10,
    total_tokens: 1030,
    cost_usd: 0.03,
    avg_latency_ms: 166.7,
  },
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ModelUsageSection", () => {
  it("renders overview cards and per-model rows with display names", async () => {
    mockApi(USAGE_PAYLOAD, [
      { provider: "openai", model_id: "gpt-4o", display_name: "GPT-4o", capabilities: {}, context_window: null, max_output_tokens: null },
    ]);
    renderSection();

    // Overview cards.
    expect(await screen.findByText("总调用数")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("1,030")).toBeTruthy();
    expect(screen.getAllByText("$0.03").length).toBe(2); // totals card + gpt-4o row
    expect(screen.getByText("模型数")).toBeTruthy();

    // Known model shows the catalog display name plus provider/model_id.
    expect(screen.getByText("GPT-4o")).toBeTruthy();
    expect(screen.getByText("openai/gpt-4o")).toBeTruthy();
    // Unknown model falls back to model_id.
    expect(screen.getByText("deepseek-chat")).toBeTruthy();
    // Null cost and latency render as dashes.
    expect(screen.getAllByText("—").length).toBe(2);
  });

  it("shows an empty state when the window has no calls", async () => {
    mockApi({ days: 7, rows: [], totals: { calls: 0, input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, reasoning_tokens: 0, total_tokens: 0, cost_usd: null, avg_latency_ms: null } });
    renderSection();

    expect(await screen.findByText("暂无模型调用记录")).toBeTruthy();
  });
});
