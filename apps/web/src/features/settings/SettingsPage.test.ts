import { describe, expect, it } from "vitest";
import { embeddingIdentityForSelection, OFF_EMBEDDING_IDENTITY, ragReadiness } from "./SettingsPage";
import type { EmbeddingConfig } from "../../lib/api/settings";

// Regression tests for the settings page embedding hint logic: the frontend
// identity must mirror proseforge/application/retrieval/indexing.py
// embedding_identity, or the "保存后将清空并重索引" hint misfires.
describe("embeddingIdentityForSelection", () => {
  it("maps engine=off to the backend OFF_IDENTITY, not null", () => {
    // An index already built with engine=off carries identity "none"; the
    // selection must compare equal so no reindex hint is shown.
    expect(OFF_EMBEDDING_IDENTITY).toBe("none");
    expect(
      embeddingIdentityForSelection("off", { localModel: "BAAI/bge-small-zh-v1.5", provider: "", model: "" }),
    ).toBe("none");
  });

  it("builds the local identity from the selected model", () => {
    expect(
      embeddingIdentityForSelection("local", {
        localModel: "intfloat/multilingual-e5-large",
        provider: "",
        model: "",
      }),
    ).toBe("local/intfloat/multilingual-e5-large");
  });

  it("builds the api identity from provider/model, trimming the model", () => {
    expect(
      embeddingIdentityForSelection("api", { localModel: "", provider: "openai", model: " text-embedding-v4 " }),
    ).toBe("openai/text-embedding-v4");
  });

  it("returns null while the api selection is incomplete", () => {
    expect(embeddingIdentityForSelection("api", { localModel: "", provider: "", model: "m" })).toBeNull();
    expect(embeddingIdentityForSelection("api", { localModel: "", provider: "openai", model: "  " })).toBeNull();
  });

  it("appends the dedicated base_url host to the api identity", () => {
    // Mirrors the backend: a dedicated base_url joins the identity via its
    // host, so switching the embedding gateway shows the reindex hint.
    expect(
      embeddingIdentityForSelection("api", {
        localModel: "",
        provider: "openai",
        model: "embed-1",
        baseUrl: "https://embed.example.com/v1",
      }),
    ).toBe("openai/embed-1@embed.example.com");
  });

  it("ignores empty or invalid base_url values", () => {
    expect(
      embeddingIdentityForSelection("api", { localModel: "", provider: "openai", model: "embed-1", baseUrl: "  " }),
    ).toBe("openai/embed-1");
    expect(
      embeddingIdentityForSelection("api", { localModel: "", provider: "openai", model: "embed-1", baseUrl: "not a url" }),
    ).toBe("openai/embed-1");
  });
});

// 状态徽标必须真实反映已保存配置（而非表单选择）的就绪状态。
function config(partial: Partial<EmbeddingConfig>): EmbeddingConfig {
  return {
    engine: "off",
    provider: null,
    model: null,
    credential_provider: null,
    base_url: null,
    local_model: "BAAI/bge-m3",
    local: { status: "not_downloaded", error: null, model: "BAAI/bge-m3", size_mb: 700, dimension: 1024 },
    indexed_model: null,
    ...partial,
  };
}

describe("ragReadiness", () => {
  it("reports loading while the config is absent", () => {
    expect(ragReadiness(undefined).tone).toBe("gray");
  });

  it("reports off as closed keyword-only retrieval", () => {
    expect(ragReadiness(config({ engine: "off" })).label).toContain("已关闭");
  });

  it("reports a ready local engine as running", () => {
    const ready = config({
      engine: "local",
      local: { status: "ready", error: null, model: "BAAI/bge-m3", size_mb: 700, dimension: 1024 },
    });
    expect(ragReadiness(ready)).toEqual({ tone: "green", label: "运行中 · 模型就绪" });
  });

  it("flags a ready local engine with a stale index as reindexing", () => {
    const stale = config({
      engine: "local",
      local: { status: "ready", error: null, model: "BAAI/bge-m3", size_mb: 700, dimension: 1024 },
      indexed_model: "local/intfloat/multilingual-e5-large",
    });
    expect(ragReadiness(stale).tone).toBe("yellow");
    expect(ragReadiness(stale).label).toContain("索引重建中");
  });

  it("shows download progress while the local model downloads", () => {
    const downloading = config({
      engine: "local",
      local: { status: "downloading", error: null, model: "BAAI/bge-m3", size_mb: 700, dimension: 1024, progress: 0.42 },
    });
    expect(ragReadiness(downloading).label).toBe("启动中 · 模型下载中 42%");
  });

  it("reports a local download error as failed", () => {
    const failed = config({
      engine: "local",
      local: { status: "error", error: "boom", model: "BAAI/bge-m3", size_mb: 700, dimension: 1024 },
    });
    expect(ragReadiness(failed).tone).toBe("red");
  });

  it("reports a configured api engine as enabled", () => {
    const api = config({ engine: "api", provider: "dashscope", model: "text-embedding-v4" });
    const readiness = ragReadiness(api);
    expect(readiness.tone).toBe("green");
    expect(readiness.label).toContain("阿里百炼");
  });

  it("reports an incomplete api engine as not ready", () => {
    expect(ragReadiness(config({ engine: "api", provider: "dashscope", model: null })).tone).toBe("yellow");
  });
});

// 本地模型选择列表由后端 visible_models 驱动（bge-m3 收敛：注册表隐藏其他
// 模型），前端不再硬编码模型目录。
import { formatLocalModelSize, localModelOptions } from "./SettingsPage";

describe("localModelOptions", () => {
  it("renders the backend's visible_models list (bge-m3 only)", () => {
    const options = localModelOptions(
      config({
        visible_models: [{ id: "BAAI/bge-m3", size_mb: 700, dimension: 1024, chunk_chars: 1200 }],
      }),
    );
    expect(options).toEqual([{ id: "BAAI/bge-m3", size_mb: 700, dimension: 1024 }]);
  });

  it("falls back to the local_models status map on older backends", () => {
    const options = localModelOptions(
      config({
        local_models: {
          "BAAI/bge-m3": { status: "ready", error: null, model: "BAAI/bge-m3", size_mb: 700, dimension: 1024 },
        },
      }),
    );
    expect(options).toEqual([{ id: "BAAI/bge-m3", size_mb: 700, dimension: 1024 }]);
  });

  it("is empty before the config arrives", () => {
    expect(localModelOptions(undefined)).toEqual([]);
  });
});

describe("formatLocalModelSize", () => {
  it("formats sub-GB sizes as MB and larger ones as GB", () => {
    expect(formatLocalModelSize(700)).toBe("约 700MB");
    expect(formatLocalModelSize(90)).toBe("约 90MB");
    expect(formatLocalModelSize(2100)).toBe("约 2.1GB");
  });
});
