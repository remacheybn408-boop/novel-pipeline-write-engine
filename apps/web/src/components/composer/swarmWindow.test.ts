import { describe, expect, it } from "vitest";
import { resolveSwarmRoleRefs, swarmContextWindow, type ModelRef, type SwarmClusterConfig } from "./swarmWindow";
import type { ModelInfo } from "../../lib/api/models";
import type { ClusterRoleConfig } from "../../lib/api/settings";

function model(provider: string, modelId: string, contextWindow: number | null): ModelInfo {
  return {
    provider,
    model_id: modelId,
    display_name: modelId,
    capabilities: {},
    context_window: contextWindow,
    max_output_tokens: null,
  };
}

function config(roles: SwarmClusterConfig["roles"], mode: "normal" | "cluster" = "cluster"): SwarmClusterConfig {
  return { mode, roles };
}

const auto: ClusterRoleConfig = "auto";

// The catalog doubles as the available pool (available_only default), e.g.
// kimi-k2.6 as the stale single-model pick and deepseek-v4-flash at 700K.
const models = [
  model("moonshot", "kimi-k2.6", 262_144),
  model("deepseek", "deepseek-v4-flash", 700_000),
  model("openai", "gpt-4.1-mini", 128_000),
];
const requested: ModelRef = { provider: "moonshot", model: "kimi-k2.6" };

describe("resolveSwarmRoleRefs", () => {
  it("keeps fully explicit seats as configured", () => {
    const refs = resolveSwarmRoleRefs(
      config({
        orchestrator: { provider: "openai", model: "gpt-4.1-mini" },
        analyst: { provider: "deepseek", model: "deepseek-v4-flash" },
        write: { provider: "moonshot", model: "kimi-k2.6" },
        review: { provider: "deepseek", model: "deepseek-v4-flash" },
        revise: { provider: "openai", model: "gpt-4.1-mini" },
      }),
      models,
      requested,
    );
    expect(refs).toEqual([
      { provider: "openai", model: "gpt-4.1-mini" },
      { provider: "deepseek", model: "deepseek-v4-flash" },
      { provider: "moonshot", model: "kimi-k2.6" },
      { provider: "deepseek", model: "deepseek-v4-flash" },
      { provider: "openai", model: "gpt-4.1-mini" },
    ]);
  });

  it("degrades auto seats: orchestrator/analyst follow write, review/revise pick the first non-write pool model", () => {
    const refs = resolveSwarmRoleRefs(
      config({ write: { provider: "moonshot", model: "kimi-k2.6" }, review: auto, revise: auto }),
      models,
      requested,
    );
    // Pool sorted by provider/model: deepseek first, so it backs up the kimi write seat.
    expect(refs).toEqual([
      { provider: "moonshot", model: "kimi-k2.6" },
      { provider: "moonshot", model: "kimi-k2.6" },
      { provider: "moonshot", model: "kimi-k2.6" },
      { provider: "deepseek", model: "deepseek-v4-flash" },
      { provider: "deepseek", model: "deepseek-v4-flash" },
    ]);
  });

  it("falls back to the requested model for an auto write seat", () => {
    const refs = resolveSwarmRoleRefs(config({ write: auto, review: auto, revise: auto }), models, requested);
    expect(refs?.[2]).toEqual(requested);
  });

  it("falls back to the first pool model when the requested model is unrunnable", () => {
    const refs = resolveSwarmRoleRefs(config({ write: auto, review: auto, revise: auto }), models, {
      provider: "gone",
      model: "deleted-model",
    });
    expect(refs?.[2]).toEqual({ provider: "deepseek", model: "deepseek-v4-flash" });
  });

  it("degrades an explicit seat whose model left the catalog like an auto seat", () => {
    const refs = resolveSwarmRoleRefs(
      config({
        write: { provider: "moonshot", model: "kimi-k2.6" },
        review: { provider: "gone", model: "deleted-model" },
        revise: auto,
      }),
      models,
      requested,
    );
    expect(refs?.[3]).toEqual({ provider: "deepseek", model: "deepseek-v4-flash" });
  });

  it("returns null in normal mode or with an empty pool (caller falls back)", () => {
    expect(resolveSwarmRoleRefs(config({ write: auto, review: auto, revise: auto }, "normal"), models, requested)).toBeNull();
    expect(resolveSwarmRoleRefs(config({ write: auto, review: auto, revise: auto }), [], requested)).toBeNull();
  });
});

describe("swarmContextWindow", () => {
  it("takes the smallest window across the five resolved seats", () => {
    const window = swarmContextWindow(
      config({
        write: { provider: "deepseek", model: "deepseek-v4-flash" },
        review: { provider: "openai", model: "gpt-4.1-mini" },
        revise: { provider: "moonshot", model: "kimi-k2.6" },
      }),
      models,
      requested,
    );
    expect(window).toBe(128_000);
  });

  it("follows the cluster config, not the stale single-model pick", () => {
    // Stale localStorage pick is gpt-4.1-mini (128K); the cluster seats are
    // deepseek (700K) + kimi backup (262,144), so the ring must show 262,144
    // — not the stale pick's 128K.
    const window = swarmContextWindow(
      config({ write: { provider: "deepseek", model: "deepseek-v4-flash" }, review: auto, revise: auto }),
      models,
      { provider: "openai", model: "gpt-4.1-mini" },
    );
    expect(window).toBe(262_144);
  });

  it("skips seats whose model carries no known window", () => {
    const window = swarmContextWindow(
      config({
        write: { provider: "deepseek", model: "deepseek-v4-flash" },
        review: { provider: "openai", model: "gpt-4.1-mini" },
        revise: { provider: "openai", model: "gpt-4.1-mini" },
      }),
      [model("deepseek", "deepseek-v4-flash", null), model("openai", "gpt-4.1-mini", 128_000)],
      requested,
    );
    expect(window).toBe(128_000);
  });

  it("returns null when no seat window is known or the config is unusable", () => {
    const unknown = [model("deepseek", "deepseek-v4-flash", null), model("openai", "gpt-4.1-mini", null)];
    expect(
      swarmContextWindow(config({ write: auto, review: auto, revise: auto }), unknown, requested),
    ).toBeNull();
    expect(swarmContextWindow(config({ write: auto, review: auto, revise: auto }, "normal"), models, requested)).toBeNull();
    expect(swarmContextWindow(config({ write: auto, review: auto, revise: auto }), [], requested)).toBeNull();
  });
});
