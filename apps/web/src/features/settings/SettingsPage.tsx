import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  addCustomModel,
  deleteCredential,
  downloadLocalEmbeddingModel,
  getEmbeddingConfig,
  listCredentials,
  listProviders,
  probeProvider,
  putEmbeddingConfig,
  saveCredential,
  syncModels,
  type CredentialInfo,
  type EmbeddingConfig,
  type EmbeddingConfigInput,
  type EmbeddingEngine,
  type EmbeddingLocalInfo,
} from "../../lib/api/settings";
import { deleteModel, listModels, type ModelInfo } from "../../lib/api/models";
import { ApiError } from "../../lib/api/client";
import { PROVIDER_BASE_URL_HINTS, providerLabel, providerSupportsEmbedding } from "../../lib/providers";
import { ChevronDownIcon, TrashIcon, XIcon } from "../../components/ui/icons";

const inputClass =
  "h-10 w-full rounded-xl border border-line bg-white px-3.5 text-sm text-ink outline-none transition-colors placeholder:text-ink-secondary focus:border-ink-secondary";

interface ProviderFeedback {
  ok: boolean;
  text: string;
}

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "操作失败，请稍后重试";
}

// Local embedding model picker is backend-driven: the registry in
// proseforge/infrastructure/embeddings/local.py decides which models are
// visible (bge-m3 convergence), and GET /settings/embedding exposes them as
// visible_models. Never hardcode the catalog here.
export interface LocalModelOption {
  id: string;
  size_mb: number;
  dimension: number;
}

export function localModelOptions(config: EmbeddingConfig | undefined): LocalModelOption[] {
  if (!config) return [];
  if (config.visible_models && config.visible_models.length > 0) {
    return config.visible_models.map((item) => ({
      id: item.id,
      size_mb: item.size_mb,
      dimension: item.dimension,
    }));
  }
  // Older backend without visible_models: fall back to the local_models
  // status map keys (still backend-driven).
  return Object.values(config.local_models ?? {}).map((info) => ({
    id: info.model,
    size_mb: info.size_mb,
    dimension: info.dimension,
  }));
}

// "约 0.7GB" / "约 90MB" — derived from the backend's size_mb.
export function formatLocalModelSize(sizeMb: number): string {
  return sizeMb >= 1024 ? `约 ${(sizeMb / 1024).toFixed(1)}GB` : `约 ${Math.round(sizeMb)}MB`;
}

const ENGINE_OPTIONS: { value: EmbeddingEngine; label: string; hint: string }[] = [
  { value: "local", label: "本地内置（推荐）", hint: "开箱即用，无需 API，向量在本地生成" },
  { value: "api", label: "供应商 API", hint: "使用已配置凭证的供应商 embedding 接口" },
  { value: "off", label: "关闭", hint: "退回纯关键词检索" },
];

// One-line summary of the saved config, shown at the top of the card.
function embeddingConfigSummary(config: EmbeddingConfig | undefined): string {
  if (!config) return "加载中…";
  if (config.engine === "off") return "已关闭（纯关键词检索）";
  if (config.engine === "local") return `本地内置 / ${config.local_model}`;
  return `${providerLabel(config.provider ?? "")} / ${config.model ?? "—"}`;
}

// Mirrors proseforge/application/retrieval/indexing.py (OFF_IDENTITY): the
// "off" engine still writes keyword-only chunks under the identity "none".
export const OFF_EMBEDDING_IDENTITY = "none";

// Backend identity strings (proseforge/application/retrieval/indexing.py
// embedding_identity): local -> "local/{model}", api -> "{provider}/{model}"
// (plus "@{host}" when a dedicated base_url is set), off -> "none". Compared
// exactly against indexed_model; a mismatch hints at the reindex-on-save.
// Null when the API-engine selection is incomplete.
export function embeddingIdentityForSelection(
  engine: EmbeddingEngine,
  selection: { localModel: string; provider: string; model: string; baseUrl?: string },
): string | null {
  if (engine === "local") return `local/${selection.localModel}`;
  if (engine === "off") return OFF_EMBEDDING_IDENTITY;
  if (selection.provider && selection.model.trim()) {
    let identity = `${selection.provider}/${selection.model.trim()}`;
    const host = hostOfUrl(selection.baseUrl);
    if (host) identity = `${identity}@${host}`;
    return identity;
  }
  return null;
}

// Mirrors the backend's urlparse(base_url).netloc; invalid URLs carry no host.
function hostOfUrl(baseUrl: string | undefined): string | null {
  if (!baseUrl?.trim()) return null;
  try {
    return new URL(baseUrl).host || null;
  } catch {
    return null;
  }
}

export interface RagReadiness {
  tone: "gray" | "green" | "yellow" | "red";
  label: string;
}

/**
 * 状态徽标：真实反映「已保存配置」的就绪状态（不是表单里未保存的选择）。
 * - off：已关闭（纯关键词检索）
 * - local：按后端回报的模型状态——就绪/下载中（含进度）/未下载/失败；
 *   模型就绪但已有索引身份不一致时提示索引重建中
 * - api：供应商+模型齐全即「已启用」（远端连通性无法在此验证，不夸口）
 */
export function ragReadiness(config: EmbeddingConfig | undefined): RagReadiness {
  if (!config) return { tone: "gray", label: "加载中…" };
  if (config.engine === "off") return { tone: "gray", label: "已关闭 · 纯关键词检索" };
  if (config.engine === "local") {
    const status = config.local.status;
    if (status === "ready") {
      const identity = embeddingIdentityForSelection("local", {
        localModel: config.local_model,
        provider: "",
        model: "",
      });
      if (config.indexed_model && identity && config.indexed_model !== identity) {
        return { tone: "yellow", label: "就绪 · 索引重建中" };
      }
      return { tone: "green", label: "运行中 · 模型就绪" };
    }
    if (status === "downloading") {
      const pct = typeof config.local.progress === "number" ? ` ${Math.round(config.local.progress * 100)}%` : "";
      return { tone: "yellow", label: `启动中 · 模型下载中${pct}` };
    }
    if (status === "error") return { tone: "red", label: "启动失败 · 模型下载出错" };
    return { tone: "yellow", label: "未就绪 · 模型未下载" };
  }
  if (config.provider && config.model) {
    const identity = embeddingIdentityForSelection("api", {
      localModel: "",
      provider: config.provider,
      model: config.model,
      baseUrl: config.base_url ?? "",
    });
    if (config.indexed_model && identity && config.indexed_model !== identity) {
      return { tone: "yellow", label: "已启用 · 索引重建中" };
    }
    return { tone: "green", label: `已启用 · ${providerLabel(config.provider)}` };
  }
  return { tone: "yellow", label: "未就绪 · 供应商配置不完整" };
}

// Settings sections split into two tabs, same inline-tabs pattern as
// PluginsPage (TABS array + state + short underline indicator).
const SETTINGS_TABS = [
  { value: "general", label: "通用模型" },
  { value: "embedding", label: "向量模型（RAG）" },
] as const;

type SettingsTab = (typeof SETTINGS_TABS)[number]["value"];

export function SettingsPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [tab, setTab] = useState<SettingsTab>("general");

  const providersQuery = useQuery({ queryKey: ["providers"], queryFn: listProviders });
  const credentialsQuery = useQuery({ queryKey: ["credentials"], queryFn: listCredentials });
  const modelsQuery = useQuery({ queryKey: ["models"], queryFn: () => listModels() });

  const providers = providersQuery.data ?? [];
  const credentials = credentialsQuery.data ?? [];
  const models = modelsQuery.data ?? [];

  // --- Credential form state ---
  const [provider, setProvider] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [credentialModelId, setCredentialModelId] = useState("");
  const [allowLocal, setAllowLocal] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formNotice, setFormNotice] = useState<ProviderFeedback | null>(null);

  // --- Credential list delete state ---
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [listNotice, setListNotice] = useState<ProviderFeedback | null>(null);

  // --- Manual model delete state (provider:model_id key) ---
  const [deletingModelKey, setDeletingModelKey] = useState<string | null>(null);

  // --- Embedding (RAG) config state ---
  const embeddingQuery = useQuery({
    queryKey: ["embedding-config"],
    queryFn: getEmbeddingConfig,
    // While the local model is downloading, poll until it leaves "downloading".
    refetchInterval: (query) => (query.state.data?.local.status === "downloading" ? 3000 : false),
  });
  const [embeddingEngine, setEmbeddingEngine] = useState<EmbeddingEngine>("local");
  const [embeddingProvider, setEmbeddingProvider] = useState("");
  const [embeddingModel, setEmbeddingModel] = useState("");
  // Dedicated embedding credential inputs. The key is never echoed back:
  // blank means "keep the saved one" (placeholder semantics).
  const [embeddingBaseUrl, setEmbeddingBaseUrl] = useState("");
  const [embeddingApiKey, setEmbeddingApiKey] = useState("");
  const [embeddingLocalModel, setEmbeddingLocalModel] = useState<string>("");
  const [embeddingSaving, setEmbeddingSaving] = useState(false);
  const [downloadStarting, setDownloadStarting] = useState(false);
  const [embeddingNotice, setEmbeddingNotice] = useState<ProviderFeedback | null>(null);
  const [embeddingInitialized, setEmbeddingInitialized] = useState(false);

  // Populate the form when the config first arrives. Subsequent refetches
  // (e.g. the 3s poll while the local model downloads) must not clobber
  // edits the user is making.
  useEffect(() => {
    const config = embeddingQuery.data;
    if (!config || embeddingInitialized) return;
    setEmbeddingEngine(config.engine);
    setEmbeddingProvider(config.provider ?? "");
    setEmbeddingModel(config.model ?? "");
    setEmbeddingBaseUrl(config.base_url ?? "");
    // The picker only offers the backend's visible models: a saved but now
    // hidden model falls back to the first visible option (bge-m3).
    const options = localModelOptions(config);
    if (config.local_model && options.some((option) => option.id === config.local_model)) {
      setEmbeddingLocalModel(config.local_model);
    } else if (options.length > 0) {
      setEmbeddingLocalModel(options[0].id);
    } else if (config.local_model) {
      setEmbeddingLocalModel(config.local_model);
    }
    setEmbeddingInitialized(true);
  }, [embeddingQuery.data, embeddingInitialized]);

  // Per-model status cache. Fresh download-trigger responses land here and
  // take precedence; otherwise the backend's per-model disk-truth map
  // (local_models) answers for any selected model, saved or not.
  const [localStatusByModel, setLocalStatusByModel] = useState<Record<string, EmbeddingLocalInfo>>({});

  useEffect(() => {
    const config = embeddingQuery.data;
    if (!config) return;
    setLocalStatusByModel((prev) => ({ ...prev, [config.local_model]: config.local }));
  }, [embeddingQuery.data]);

  // The config poll (refetchInterval above) only tracks the saved model.
  // When the user starts downloading a different, not-yet-saved model,
  // refresh its status via the idempotent download endpoint until it settles.
  const savedLocalModel = embeddingQuery.data?.local_model;
  useEffect(() => {
    const status = localStatusByModel[embeddingLocalModel];
    if (status?.status !== "downloading" || embeddingLocalModel === savedLocalModel) return;
    const timer = window.setInterval(() => {
      downloadLocalEmbeddingModel(embeddingLocalModel)
        .then((info) => setLocalStatusByModel((prev) => ({ ...prev, [embeddingLocalModel]: info })))
        .catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [embeddingLocalModel, savedLocalModel, localStatusByModel]);

  // --- Per-provider sync/probe state ---
  const [pendingAction, setPendingAction] = useState<Record<string, "sync" | "probe" | null>>({});
  const [feedback, setFeedback] = useState<Record<string, ProviderFeedback>>({});
  // Expanded state per provider group; unset entries fall back to the default
  // (expanded when credentials are configured, collapsed otherwise).
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({});
  // Section-level collapse for the whole "可用模型" block (default expanded).
  const [modelsSectionOpen, setModelsSectionOpen] = useState(true);
  // Whether unconfigured providers are shown in the list (default hidden).
  const [showAllProviders, setShowAllProviders] = useState(false);

  const configuredProviders = new Set(credentials.map((c) => c.provider));

  // The provider the credential form currently targets (drives placeholders).
  const effectiveProvider = provider || providers[0]?.id || "";

  async function handleSaveCredential(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const providerId = effectiveProvider;
    if (!providerId || !apiKey.trim() || saving) return;
    setSaving(true);
    setFormNotice(null);
    const modelId = credentialModelId.trim();
    try {
      await saveCredential({
        provider: providerId,
        api_key: apiKey.trim(),
        ...(baseUrl.trim() ? { base_url: baseUrl.trim() } : {}),
        ...(allowLocal ? { allow_local: true } : {}),
      });
      setApiKey("");
      // Optionally register a model alongside the credential (for platforms
      // whose model list cannot be auto-discovered). The context window is
      // resolved backend-side (known windows / catalog), never user-entered.
      if (modelId) {
        try {
          await addCustomModel({
            provider: providerId,
            model_id: modelId,
          });
          setCredentialModelId("");
          setFormNotice({ ok: true, text: `凭证已保存，模型 ${modelId} 已添加` });
        } catch (err) {
          setFormNotice({ ok: false, text: `凭证已保存，但模型添加失败：${errorText(err)}` });
        }
      } else {
        setFormNotice({ ok: true, text: "凭证已保存，模型同步已在后台开始" });
      }
      // The backend enqueues a model sync after saving; refresh both lists.
      await queryClient.invalidateQueries({ queryKey: ["credentials"] });
      await queryClient.invalidateQueries({ queryKey: ["models"] });
    } catch (err) {
      setFormNotice({ ok: false, text: `保存失败：${errorText(err)}` });
    } finally {
      setSaving(false);
    }
  }

  async function handleDeleteCredential(credential: CredentialInfo) {
    if (deletingId) return;
    // Destructive and irreversible: always confirm first.
    if (!window.confirm(`确定删除 ${providerLabel(credential.provider)} 的凭证？删除后需重新填写才能继续使用该提供商。`)) {
      return;
    }
    setDeletingId(credential.id);
    setListNotice(null);
    try {
      await deleteCredential(credential.id);
      setListNotice({ ok: true, text: `已删除 ${providerLabel(credential.provider)} 的凭证` });
      await queryClient.invalidateQueries({ queryKey: ["credentials"] });
      // The chat model selector reads ["models"]; dropping the last
      // credential hides the provider's synced models there too.
      await queryClient.invalidateQueries({ queryKey: ["models"] });
    } catch (err) {
      // Includes 409 (credential still referenced): show the backend's reason.
      setListNotice({ ok: false, text: `删除失败：${errorText(err)}` });
    } finally {
      setDeletingId(null);
    }
  }

  async function handleDeleteModel(model: ModelInfo) {
    const key = `${model.provider}:${model.model_id}`;
    if (deletingModelKey) return;
    // Destructive and irreversible: always confirm first.
    if (!window.confirm(`确定删除手动添加的模型 ${model.display_name}？删除后需重新添加才能继续使用。`)) {
      return;
    }
    setDeletingModelKey(key);
    setListNotice(null);
    try {
      await deleteModel(model.provider, model.model_id);
      setListNotice({ ok: true, text: `已删除模型 ${model.display_name}` });
      // The chat model selector reads ["models"]; a deleted pick there falls
      // back to the first available model automatically (ModelSelect).
      await queryClient.invalidateQueries({ queryKey: ["models"] });
    } catch (err) {
      setListNotice({ ok: false, text: `删除失败：${errorText(err)}` });
    } finally {
      setDeletingModelKey(null);
    }
  }

  // Assemble the PUT body for the currently selected engine.
  function embeddingPayload(force: boolean): EmbeddingConfigInput {
    let payload: EmbeddingConfigInput;
    if (embeddingEngine === "api") {
      payload = { engine: "api", provider: embeddingProvider, model: embeddingModel.trim() };
      // A fresh key means (re)save the dedicated credential; blank key means
      // keep the stored one, so base_url is only sent alongside a key.
      if (embeddingApiKey.trim()) {
        payload.api_key = embeddingApiKey.trim();
        payload.base_url = embeddingBaseUrl.trim();
      }
    } else if (embeddingEngine === "local") {
      payload = { engine: "local", local_model: embeddingLocalModel };
    } else {
      payload = { engine: "off" };
    }
    if (force) payload.force = true;
    return payload;
  }

  async function saveEmbedding(force: boolean) {
    if (embeddingSaving) return;
    setEmbeddingSaving(true);
    setEmbeddingNotice(null);
    try {
      const saved = await putEmbeddingConfig(embeddingPayload(force));
      // Never keep a submitted key in the form; the placeholder takes over.
      setEmbeddingApiKey("");
      // 启动是「真启动」：本地引擎保存后若模型未就绪，顺手触发后台下载
      // (idempotent) —— 不再需要用户先点「下载」再点「启动」。
      if (saved.engine === "local" && saved.local.status !== "ready") {
        try {
          const info = await downloadLocalEmbeddingModel(saved.local_model);
          setLocalStatusByModel((prev) => ({ ...prev, [saved.local_model]: info }));
          setEmbeddingNotice({ ok: true, text: "已保存，模型下载中，就绪后 RAG 即可使用" });
        } catch {
          setEmbeddingNotice({ ok: true, text: "已保存；模型下载启动失败，请点「下载」重试" });
        }
      } else if (saved.engine === "off") {
        setEmbeddingNotice({ ok: true, text: "已关闭 RAG 检索（纯关键词模式）" });
      } else {
        setEmbeddingNotice({ ok: true, text: "已保存，RAG 已启用" });
      }
      await queryClient.invalidateQueries({ queryKey: ["embedding-config"] });
    } catch (err) {
      // 409: the new identity differs from the existing index — confirm
      // (page convention: window.confirm, same as credential delete), then
      // resend with force=true to drop and reindex.
      if (!force && err instanceof ApiError && err.status === 409) {
        if (window.confirm(`${err.message}\n\n确认切换？已有索引将被清空并重索引。`)) {
          await saveEmbedding(true);
        }
      } else {
        // 400 (provider has no credential) and other errors: show the reason.
        setEmbeddingNotice({ ok: false, text: `保存失败：${errorText(err)}` });
      }
    } finally {
      setEmbeddingSaving(false);
    }
  }

  // 关闭按钮：无论表单当前选什么，直接把引擎关掉（engine=off 落库）。
  async function handleDisableRag(force = false) {
    if (embeddingSaving) return;
    setEmbeddingSaving(true);
    setEmbeddingNotice(null);
    try {
      await putEmbeddingConfig(force ? { engine: "off", force: true } : { engine: "off" });
      setEmbeddingEngine("off");
      setEmbeddingNotice({ ok: true, text: "已关闭 RAG 检索（纯关键词模式）" });
      await queryClient.invalidateQueries({ queryKey: ["embedding-config"] });
    } catch (err) {
      if (!force && err instanceof ApiError && err.status === 409) {
        if (window.confirm(`${err.message}\n\n确认关闭？已有索引将被清空。`)) {
          await handleDisableRag(true);
          return;
        }
      } else {
        setEmbeddingNotice({ ok: false, text: `关闭失败：${errorText(err)}` });
      }
    } finally {
      setEmbeddingSaving(false);
    }
  }

  async function handleSaveEmbedding(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (embeddingEngine === "api" && (!embeddingProvider || !embeddingModel.trim())) return;
    // A fresh dedicated key requires its endpoint too (backend enforces 400).
    if (embeddingEngine === "api" && embeddingApiKey.trim() && !embeddingBaseUrl.trim()) return;
    await saveEmbedding(false);
  }

  // Kick off the background model download, then invalidate so the existing
  // 3s poll (active while status = "downloading") takes over the progress.
  async function handleDownloadLocalModel() {
    if (downloadStarting) return;
    setDownloadStarting(true);
    setEmbeddingNotice(null);
    try {
      const info = await downloadLocalEmbeddingModel(embeddingLocalModel);
      // Track the status of the model the user actually selected; the config
      // endpoint only reports the saved one.
      setLocalStatusByModel((prev) => ({ ...prev, [embeddingLocalModel]: info }));
      await queryClient.invalidateQueries({ queryKey: ["embedding-config"] });
    } catch (err) {
      setEmbeddingNotice({ ok: false, text: `下载失败：${errorText(err)}` });
    } finally {
      setDownloadStarting(false);
    }
  }

  async function runProviderAction(providerId: string, action: "sync" | "probe") {
    if (pendingAction[providerId]) return;
    setPendingAction((prev) => ({ ...prev, [providerId]: action }));
    try {
      if (action === "sync") {
        const result = await syncModels(providerId);
        setFeedback((prev) => ({ ...prev, [providerId]: { ok: true, text: `同步成功，共同步 ${result.count} 个模型` } }));
        await queryClient.invalidateQueries({ queryKey: ["models"] });
      } else {
        await probeProvider(providerId);
        setFeedback((prev) => ({ ...prev, [providerId]: { ok: true, text: "连接正常" } }));
      }
    } catch (err) {
      setFeedback((prev) => ({
        ...prev,
        [providerId]: { ok: false, text: `${action === "sync" ? "同步失败" : "连接失败"}：${errorText(err)}` },
      }));
    } finally {
      setPendingAction((prev) => ({ ...prev, [providerId]: null }));
    }
  }

  const actionButtonClass =
    "rounded-lg border border-line bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50";

  // "可用模型" only lists configured providers by default; the rest hide
  // behind a toggle. Fresh installs (nothing configured) show everything.
  const configuredList = providers.filter((item) => configuredProviders.has(item.id));
  const unconfiguredCount = providers.length - configuredList.length;
  const visibleProviders =
    configuredList.length === 0 || showAllProviders ? providers : configuredList;

  // Embedding provider options: providers that already have a credential,
  // filtered to vendors that actually expose an embeddings endpoint — a
  // chat-only vendor (e.g. 深度求索) could never serve the vector engine.
  const credentialProviders = [...new Set(credentials.map((c) => c.provider))];
  // A dedicated embedding key makes the engine self-contained: while one is
  // being entered, any embedding-capable registered provider may be picked
  // (not just the chat-credentialed ones). A previously saved dedicated
  // provider stays selectable even without a chat credential; with no chat
  // credentials at all, every embedding-capable provider is offered
  // (dedicated-key flow).
  const savedEmbeddingProvider = embeddingQuery.data?.provider ?? "";
  const embeddingProviderOptions = [
    ...new Set([
      ...(credentialProviders.length > 0 ? credentialProviders : providers.map((item) => item.id)),
      ...(embeddingApiKey.trim() ? providers.map((item) => item.id) : []),
    ]),
  ].filter((providerId) => providerSupportsEmbedding(providerId) || providerId === savedEmbeddingProvider);

  // Backend identity strings (proseforge/application/retrieval/indexing.py
  // embedding_identity), compared exactly against indexed_model; a mismatch
  // hints at the reindex-on-save. The dedicated base_url joins the identity
  // via its host: a fresh key uses the form value, otherwise the saved one.
  const selectedEmbeddingIdentity = embeddingIdentityForSelection(embeddingEngine, {
    localModel: embeddingLocalModel,
    provider: embeddingProvider,
    model: embeddingModel,
    baseUrl: embeddingApiKey.trim() ? embeddingBaseUrl.trim() : (embeddingQuery.data?.base_url ?? ""),
  });
  const indexedModel = embeddingQuery.data?.indexed_model ?? null;
  // Backend-driven local model picker (visible registry entries only).
  const localOptions = localModelOptions(embeddingQuery.data);
  // Status of the model currently selected in the dropdown: fresh in-flight
  // states from this session win, then the backend's per-model disk truth
  // (local_models), else null (never reported).
  const localStatus =
    localStatusByModel[embeddingLocalModel] ??
    embeddingQuery.data?.local_models?.[embeddingLocalModel] ??
    null;
  const showIndexedHint =
    indexedModel !== null && (selectedEmbeddingIdentity === null || indexedModel !== selectedEmbeddingIdentity);

  return (
    <div className="mx-auto w-full max-w-[720px] px-8 py-10">
      {/* Close: exit settings back to the chat home */}
      <button
        type="button"
        title="关闭设置"
        aria-label="关闭设置"
        onClick={() => navigate("/")}
        className="fixed right-6 top-6 flex h-9 w-9 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-ink"
      >
        <XIcon size={20} />
      </button>

      <h1 className="mb-6 text-2xl font-bold text-ink">设置</h1>

      {/* Tabs: 通用模型 (credentials + available models) / 向量模型（RAG） */}
      <div className="mb-8 flex items-center gap-6 border-b border-line">
        {SETTINGS_TABS.map((item) => {
          const active = tab === item.value;
          return (
            <button
              key={item.value}
              type="button"
              onClick={() => setTab(item.value)}
              className={`relative pb-2.5 text-sm transition-colors ${
                active ? "font-semibold text-ink" : "text-ink-secondary hover:text-ink"
              }`}
            >
              {item.label}
              {active && <span className="absolute bottom-0 left-1/2 h-0.5 w-6 -translate-x-1/2 rounded-full bg-ink" />}
            </button>
          );
        })}
      </div>

      {/* Model credentials */}
      {tab === "general" && (
      <section className="mb-12">
        <h2 className="mb-1 text-base font-semibold text-ink">模型凭证</h2>
        <p className="mb-4 text-sm text-ink-secondary">配置 OpenAI 兼容接口的访问凭证，每个提供商保存一份。</p>

        <form onSubmit={handleSaveCredential} className="mb-6 flex flex-col gap-3 rounded-2xl border border-line bg-white p-5">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="credential-provider" className="text-sm text-ink">提供商</label>
            {providers.length > 0 ? (
              <select
                id="credential-provider"
                value={effectiveProvider}
                onChange={(e) => setProvider(e.target.value)}
                className={inputClass}
              >
                {providers.map((item) => (
                  <option key={item.id} value={item.id}>
                    {providerLabel(item.id)}
                    {configuredProviders.has(item.id) ? "（已配置）" : ""}
                  </option>
                ))}
              </select>
            ) : (
              <p className="text-sm text-ink-secondary">
                {providersQuery.isPending ? "加载中…" : "后端未注册任何提供商"}
              </p>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="credential-api-key" className="text-sm text-ink">API Key</label>
            <input
              id="credential-api-key"
              type="password"
              required
              autoComplete="off"
              placeholder="sk-..."
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              className={inputClass}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="credential-base-url" className="text-sm text-ink">
              Base URL <span className="text-ink-secondary">（可选，用于自定义网关或本地模型）</span>
            </label>
            <input
              id="credential-base-url"
              type="url"
              placeholder={PROVIDER_BASE_URL_HINTS[effectiveProvider] ?? "https://api.openai.com/v1"}
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              className={inputClass}
            />
            <p className="text-xs text-ink-secondary">
              其他第三方服务：只要是 OpenAI 兼容接口，选 OpenAI 并填它的 Base URL 即可接入
            </p>
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="credential-model-id" className="text-sm text-ink">
              模型名 <span className="text-ink-secondary">（可选）</span>
            </label>
            <input
              id="credential-model-id"
              type="text"
              placeholder="doubao-pro-32k 或 ep-xxxxxxxx 接入点"
              value={credentialModelId}
              onChange={(e) => setCredentialModelId(e.target.value)}
              className={inputClass}
            />
            <p className="text-xs text-ink-secondary">
              自动拉取不到模型列表的平台（如火山引擎），在此填模型名或接入点 ID，保存凭证时一并登记
            </p>
          </div>

          {baseUrl.trim() && (
            <label className="flex items-center gap-2 text-sm text-ink-secondary">
              <input
                type="checkbox"
                checked={allowLocal}
                onChange={(e) => setAllowLocal(e.target.checked)}
                className="h-4 w-4 accent-ink"
              />
              允许本地/内网地址（如 http://localhost:11434）
            </label>
          )}

          {formNotice && (
            <p className={`text-sm ${formNotice.ok ? "text-emerald-600" : "text-red-600"}`}>{formNotice.text}</p>
          )}

          <div>
            <button
              type="submit"
              disabled={saving || providers.length === 0}
              className="h-10 rounded-xl bg-ink px-5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {saving ? "保存中…" : "保存凭证"}
            </button>
          </div>
        </form>

        {/* Configured credentials */}
        {listNotice && (
          <p className={`mb-3 mt-4 text-sm ${listNotice.ok ? "text-emerald-600" : "text-red-600"}`}>{listNotice.text}</p>
        )}
        {credentials.length > 0 && (
          <ul className="divide-y divide-line rounded-2xl border border-line bg-white">
            {credentials.map((credential) => (
              <li key={credential.id} className="flex items-center justify-between px-5 py-3.5">
                <span className="text-sm font-medium text-ink">{providerLabel(credential.provider)}</span>
                <span className="flex items-center gap-2">
                  <span className="text-sm text-ink-secondary">
                    {/* The list endpoint returns the literal "configured" instead of a mask. */}
                    {credential.masked_key === "configured" ? "已配置" : credential.masked_key}
                  </span>
                  <button
                    type="button"
                    title="删除凭证"
                    disabled={deletingId === credential.id}
                    onClick={() => void handleDeleteCredential(credential)}
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-secondary transition-colors hover:bg-hover hover:text-red-600 disabled:opacity-50"
                  >
                    <TrashIcon size={16} />
                  </button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
      )}

      {/* Embedding (RAG) config */}
      {tab === "embedding" && (
      <section className="mb-12">
        <h2 className="mb-1 text-base font-semibold text-ink">向量检索（RAG）</h2>
        <p className="mb-4 text-sm text-ink-secondary">
          检索增强（RAG）使用的向量引擎。本地内置开箱即用、无需 API；关闭则退回纯关键词检索。
        </p>

        <form onSubmit={handleSaveEmbedding} className="flex flex-col gap-3 rounded-2xl border border-line bg-white p-5">
          <p className="text-xs text-ink-secondary">当前配置：{embeddingConfigSummary(embeddingQuery.data)}</p>

          {/* Engine picker */}
          <div className="flex flex-col gap-2" role="radiogroup" aria-label="向量引擎">
            {ENGINE_OPTIONS.map((option) => (
              <label
                key={option.value}
                className={`flex cursor-pointer items-start gap-3 rounded-xl border px-4 py-3 transition-colors ${
                  embeddingEngine === option.value
                    ? "border-ink-secondary bg-hover"
                    : "border-line bg-white hover:bg-hover"
                }`}
              >
                <input
                  type="radio"
                  name="embedding-engine"
                  value={option.value}
                  checked={embeddingEngine === option.value}
                  onChange={() => setEmbeddingEngine(option.value)}
                  className="mt-0.5 h-4 w-4 shrink-0 accent-ink"
                />
                <span className="flex flex-col gap-0.5">
                  <span className="text-sm font-medium text-ink">{option.label}</span>
                  <span className="text-xs text-ink-secondary">{option.hint}</span>
                </span>
              </label>
            ))}
          </div>

          {/* Local engine: model picker + download status */}
          {embeddingEngine === "local" && (
            <>
              <div className="flex flex-col gap-1.5">
                <label htmlFor="embedding-local-model" className="text-sm text-ink">本地模型</label>
                <select
                  id="embedding-local-model"
                  value={embeddingLocalModel}
                  onChange={(e) => setEmbeddingLocalModel(e.target.value)}
                  className={inputClass}
                >
                  {localOptions.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.id}（{formatLocalModelSize(item.size_mb)}，{item.dimension} 维）
                    </option>
                  ))}
                </select>
              </div>
              {localStatus && (
                <div className="flex flex-col gap-1.5">
                  <p className="flex flex-wrap items-center gap-2 text-xs text-ink-secondary">
                    模型状态：
                    {localStatus.status === "not_downloaded" && (
                      <>
                        <span className="rounded-md bg-hover px-1.5 py-0.5 text-[11px] text-ink-secondary">未下载</span>
                        <button
                          type="button"
                          disabled={downloadStarting}
                          onClick={() => void handleDownloadLocalModel()}
                          className={actionButtonClass}
                        >
                          {downloadStarting ? "启动中…" : "下载"}
                        </button>
                      </>
                    )}
                    {localStatus.status === "downloading" && (
                      <span className="inline-flex items-center gap-1.5 rounded-md bg-hover px-1.5 py-0.5 text-[11px] text-ink-secondary">
                        <span className="h-3 w-3 animate-spin rounded-full border border-ink-secondary border-t-transparent" />
                        {typeof localStatus.progress === "number"
                          ? `下载中… ${Math.round(localStatus.progress * 100)}%`
                          : "下载中…"}
                      </span>
                    )}
                    {localStatus.status === "ready" && (
                      <span className="rounded-md bg-hover px-1.5 py-0.5 text-[11px] text-emerald-600">就绪</span>
                    )}
                    {localStatus.status === "error" && (
                      <>
                        <span className="text-red-600">下载失败：{localStatus.error ?? "未知错误"}</span>
                        <button
                          type="button"
                          disabled={downloadStarting}
                          onClick={() => void handleDownloadLocalModel()}
                          className={actionButtonClass}
                        >
                          {downloadStarting ? "启动中…" : "重试"}
                        </button>
                      </>
                    )}
                  </p>
                  {localStatus.status === "downloading" && typeof localStatus.progress === "number" && (
                    <div className="h-1.5 w-full overflow-hidden rounded-full bg-hover">
                      <div
                        className="h-full rounded-full bg-ink transition-all"
                        style={{ width: `${Math.min(100, Math.round(localStatus.progress * 100))}%` }}
                      />
                    </div>
                  )}
                </div>
              )}
              {/* Fallback for an older backend without local_models: offer an
                  explicit download/status check instead of showing nothing. */}
              {!localStatus && (
                <p className="flex flex-wrap items-center gap-2 text-xs text-ink-secondary">
                  该模型状态暂未回报（旧版后端），可点击「下载」获取真实状态。
                  <button
                    type="button"
                    disabled={downloadStarting}
                    onClick={() => void handleDownloadLocalModel()}
                    className={actionButtonClass}
                  >
                    {downloadStarting ? "启动中…" : "下载"}
                  </button>
                </p>
              )}
              <p className="text-xs text-ink-secondary">无需其他配置，保存后即可使用。</p>
            </>
          )}

          {/* API engine: provider + model + optional dedicated credential */}
          {embeddingEngine === "api" && (
            <>
              <div className="flex flex-col gap-1.5">
                <label htmlFor="embedding-provider" className="text-sm text-ink">供应商</label>
                {embeddingProviderOptions.length === 0 ? (
                  <p className="text-sm text-ink-secondary">后端未注册任何提供商。</p>
                ) : (
                  <select
                    id="embedding-provider"
                    required
                    value={embeddingProvider}
                    onChange={(e) => setEmbeddingProvider(e.target.value)}
                    className={inputClass}
                  >
                    <option value="" disabled>
                      选择供应商
                    </option>
                    {embeddingProviderOptions.map((providerId) => (
                      <option key={providerId} value={providerId}>
                        {providerLabel(providerId)}
                      </option>
                    ))}
                  </select>
                )}
              </div>
              <div className="flex flex-col gap-1.5">
                <label htmlFor="embedding-model" className="text-sm text-ink">模型</label>
                <input
                  id="embedding-model"
                  required
                  type="text"
                  placeholder="doubao-embedding / text-embedding-v4 / BAAI/bge-m3"
                  value={embeddingModel}
                  onChange={(e) => setEmbeddingModel(e.target.value)}
                  className={inputClass}
                />
              </div>

              {/* Dedicated embedding credential: the vector engine can use its
                  own endpoint/key instead of sharing the chat credential. The
                  saved key is never echoed; blank keeps it. */}
              <div className="flex flex-col gap-1.5">
                <label htmlFor="embedding-base-url" className="text-sm text-ink">
                  独立 Base URL <span className="text-ink-secondary">（可选，向量模型专用接入点）</span>
                </label>
                <input
                  id="embedding-base-url"
                  type="url"
                  placeholder="https://api.openai.com/v1"
                  value={embeddingBaseUrl}
                  onChange={(e) => setEmbeddingBaseUrl(e.target.value)}
                  className={inputClass}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <label htmlFor="embedding-api-key" className="text-sm text-ink">
                  独立 API Key <span className="text-ink-secondary">（可选，不回显）</span>
                </label>
                <input
                  id="embedding-api-key"
                  type="password"
                  autoComplete="off"
                  placeholder={
                    embeddingQuery.data?.credential_provider ? "已保存独立密钥，留空保持不变" : "sk-...（留空则使用供应商凭证）"
                  }
                  value={embeddingApiKey}
                  onChange={(e) => setEmbeddingApiKey(e.target.value)}
                  className={inputClass}
                />
                {embeddingApiKey.trim() && !embeddingBaseUrl.trim() && (
                  <p className="text-xs text-amber-600">填写独立 API Key 时需同时填写独立 Base URL。</p>
                )}
              </div>

              <p className="text-xs text-ink-secondary">
                需支持 embedding 接口（DeepSeek 不支持）；切换模型或 Base URL 需重建索引。
                不填独立密钥时使用「通用模型」中已保存的供应商凭证。
              </p>
            </>
          )}

          {/* Off: warning */}
          {embeddingEngine === "off" && (
            <p className="rounded-xl border border-line bg-hover px-4 py-3 text-sm text-amber-600">
              将不对新内容生成向量，仅保留关键词检索。
            </p>
          )}

          {/* Existing index uses a different engine/model than the selection */}
          {showIndexedHint && (
            <p className="text-xs text-amber-600">
              当前已有索引使用「{indexedModel}」，与所选配置不一致，保存后将清空并重索引。
            </p>
          )}

          {/* Index reconciliation: chapters that should be indexed vs what the
              index actually holds. Drift means the read side is silently
              returning empty evidence — loud by design (P0 visibility). */}
          {embeddingQuery.data?.index_health && embeddingQuery.data.index_health.indexable_chapters > 0 && (
            <p className={`text-xs ${embeddingQuery.data.index_health.drift ? "text-red-600" : "text-ink-secondary"}`}>
              索引覆盖：应索引 {embeddingQuery.data.index_health.indexable_chapters} 章 / 已索引{" "}
              {embeddingQuery.data.index_health.indexed_documents} 章（
              {embeddingQuery.data.index_health.active_chunks} 个证据块）
              {embeddingQuery.data.index_health.drift && "——索引缺失，检索正在返回空结果，请保存一次配置以自动补建"}
            </p>
          )}

          {embeddingNotice && (
            <p className={`text-sm ${embeddingNotice.ok ? "text-emerald-600" : "text-red-600"}`}>{embeddingNotice.text}</p>
          )}
          {/* Local engine with a model that is not ready yet: 启动 will kick
              the download itself; say so. */}
          {embeddingEngine === "local" && localStatus && localStatus.status !== "ready" && (
            <p className="text-xs text-amber-600">
              所选本地模型尚未就绪，点击「启动」后将自动开始下载；也可以先点「下载」。
            </p>
          )}
          {/* 操作行：下载（仅本地引擎）/ 启动（保存+真正启用）/ 关闭 + 状态徽标 */}
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={
                embeddingEngine !== "local" ||
                downloadStarting ||
                localStatus?.status === "downloading" ||
                localStatus?.status === "ready"
              }
              onClick={() => void handleDownloadLocalModel()}
              className="h-10 rounded-xl border border-line bg-white px-5 text-sm text-ink transition-colors hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50"
            >
              {downloadStarting ? "启动中…" : "下载"}
            </button>
            <button
              type="submit"
              disabled={
                embeddingSaving ||
                (embeddingEngine === "api" && embeddingProviderOptions.length === 0) ||
                (embeddingEngine === "api" && embeddingApiKey.trim() !== "" && !embeddingBaseUrl.trim())
              }
              className="h-10 rounded-xl bg-ink px-5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {embeddingSaving ? "启动中…" : "启动"}
            </button>
            <button
              type="button"
              disabled={embeddingSaving || embeddingQuery.data?.engine === "off"}
              onClick={() => void handleDisableRag()}
              className="h-10 rounded-xl border border-line bg-white px-5 text-sm text-red-600 transition-colors hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50"
            >
              关闭
            </button>
            <span
              className={`ml-auto rounded-md px-2 py-1 text-[11px] ${
                {
                  gray: "bg-hover text-ink-secondary",
                  green: "bg-emerald-50 text-emerald-600",
                  yellow: "bg-amber-50 text-amber-600",
                  red: "bg-red-50 text-red-600",
                }[ragReadiness(embeddingQuery.data).tone]
              }`}
            >
              状态：{ragReadiness(embeddingQuery.data).label}
            </span>
          </div>
        </form>
      </section>
      )}

      {/* Available models */}
      {tab === "general" && (
      <section>
        <div
          role="button"
          tabIndex={0}
          onClick={() => setModelsSectionOpen((v) => !v)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              setModelsSectionOpen((v) => !v);
            }
          }}
          className="mb-1 flex cursor-pointer items-center justify-between"
        >
          <h2 className="text-base font-semibold text-ink">可用模型</h2>
          <ChevronDownIcon
            size={16}
            className={`text-ink-secondary transition-transform ${modelsSectionOpen ? "rotate-180" : ""}`}
          />
        </div>
        <p className="mb-4 text-sm text-ink-secondary">同步提供商的模型目录，或测试凭证连通性。</p>

        {modelsSectionOpen && (
          <>
            {providers.length === 0 && !providersQuery.isPending && (
              <p className="text-sm text-ink-secondary">后端未注册任何提供商。</p>
            )}

            <div className="flex flex-col gap-4">
              {visibleProviders.map((item) => {
            const providerModels = models.filter((m) => m.provider === item.id);
            const state = feedback[item.id];
            const pending = pendingAction[item.id];
            const expanded = expandedGroups[item.id] ?? configuredProviders.has(item.id);
            return (
              <div key={item.id} className="rounded-2xl border border-line bg-white">
                <div
                  role="button"
                  tabIndex={0}
                  onClick={() => setExpandedGroups((prev) => ({ ...prev, [item.id]: !expanded }))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setExpandedGroups((prev) => ({ ...prev, [item.id]: !expanded }));
                    }
                  }}
                  className="flex cursor-pointer items-center justify-between px-5 py-3.5"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-ink">{providerLabel(item.id)}</span>
                    {configuredProviders.has(item.id) ? (
                      <span className="rounded-md bg-hover px-1.5 py-0.5 text-[11px] text-ink-secondary">已配置凭证</span>
                    ) : (
                      <span className="rounded-md bg-hover px-1.5 py-0.5 text-[11px] text-ink-secondary">未配置凭证</span>
                    )}
                    <span className="text-xs text-ink-secondary">
                      {providerModels.length > 0 ? `${providerModels.length} 个模型` : "暂无模型"}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={!!pending || !configuredProviders.has(item.id)}
                      onClick={(e) => {
                        e.stopPropagation();
                        void runProviderAction(item.id, "probe");
                      }}
                      title={configuredProviders.has(item.id) ? undefined : "请先配置该提供商的凭证"}
                      className={actionButtonClass}
                    >
                      {pending === "probe" ? "测试中…" : "测试连接"}
                    </button>
                    <button
                      type="button"
                      disabled={!!pending || !configuredProviders.has(item.id)}
                      onClick={(e) => {
                        e.stopPropagation();
                        void runProviderAction(item.id, "sync");
                      }}
                      title={configuredProviders.has(item.id) ? undefined : "请先配置该提供商的凭证"}
                      className={actionButtonClass}
                    >
                      {pending === "sync" ? "同步中…" : "同步模型"}
                    </button>
                    <ChevronDownIcon
                      size={16}
                      className={`text-ink-secondary transition-transform ${expanded ? "rotate-180" : ""}`}
                    />
                  </div>
                </div>

                {state && (
                  <p className={`border-t border-line px-5 py-2.5 text-sm ${state.ok ? "text-emerald-600" : "text-red-600"}`}>
                    {state.text}
                  </p>
                )}

                {expanded &&
                  (providerModels.length === 0 ? (
                    <p className="border-t border-line px-5 py-3.5 text-sm text-ink-secondary">暂无模型，点击「同步模型」拉取</p>
                  ) : (
                    <ul className="divide-y divide-line border-t border-line">
                      {providerModels.map((model) => {
                        // Only owned manual rows are deletable: synced rows
                        // follow provider sync, legacy shared manual rows
                        // (owner_id null) are undeletable by design.
                        const deletable = model.capabilities.manual === true && model.owner_id != null;
                        return (
                        <li key={model.model_id} className="flex items-center justify-between px-5 py-3">
                          <span className="flex min-w-0 items-center gap-2">
                            <span className="truncate text-sm text-ink">{model.display_name}</span>
                            {model.capabilities.manual === true && (
                              <span className="shrink-0 rounded-md bg-hover px-1.5 py-0.5 text-[11px] text-ink-secondary">
                                手动
                              </span>
                            )}
                          </span>
                          <span className="ml-4 flex shrink-0 items-center gap-2">
                            <span className="text-xs text-ink-secondary">
                              {model.context_window ? `上下文 ${model.context_window.toLocaleString()} tokens` : model.model_id}
                            </span>
                            {deletable && (
                              <button
                                type="button"
                                disabled={!!deletingModelKey}
                                onClick={() => void handleDeleteModel(model)}
                                title="删除该手动模型"
                                className="rounded-lg p-1.5 text-ink-secondary transition-colors hover:bg-hover hover:text-red-600 disabled:opacity-50"
                              >
                                <TrashIcon size={14} />
                              </button>
                            )}
                          </span>
                        </li>
                        );
                      })}
                    </ul>
                  ))}
              </div>
            );
          })}
            </div>

            {/* Reveal the unconfigured providers (hidden by default) */}
            {configuredList.length > 0 && unconfiguredCount > 0 && (
              <div className="mt-4 flex justify-center">
                <button
                  type="button"
                  onClick={() => setShowAllProviders((v) => !v)}
                  className="rounded-full border border-line bg-white px-4 py-1.5 text-xs text-ink-secondary transition-colors hover:bg-hover hover:text-ink"
                >
                  {showAllProviders ? "收起全部提供商" : `显示全部提供商（${unconfiguredCount} 个）`}
                </button>
              </div>
            )}
          </>
        )}
      </section>
      )}
    </div>
  );
}
