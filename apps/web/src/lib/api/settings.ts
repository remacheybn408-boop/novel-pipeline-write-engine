/**
 * Settings-related endpoints — proseforge/api/routes/credentials.py
 * and proseforge/api/routes/providers.py.
 *
 * Confirmed shapes:
 *   GET  /api/v1/providers -> [{id, status}]        (id = provider registry id)
 *   GET  /api/v1/credentials -> [{id, provider, masked_key}]
 *        Note: in the list response masked_key is the literal string
 *        "configured" (only the POST response carries a real mask).
 *   POST /api/v1/credentials  {provider, api_key, base_url?, allow_local?}
 *        -> 201 {id, provider, masked_key}. Upsert semantics: one credential
 *        per provider per user. Saving also enqueues a background model sync.
 *   DELETE /api/v1/credentials/{credential_id}
 *        -> 204/200 on success; 409 with a readable reason when the
 *        credential is still referenced.
 *   POST /api/v1/providers/{provider_id}/sync-models
 *        -> {provider, count, models: [model_id...]}
 *   POST /api/v1/providers/{provider_id}/probe
 *        -> {provider, ...provider-specific result}
 *        Both return 400 when credentials are missing, 404 for unknown
 *        providers and 502 when the upstream call fails.
 *   GET  /api/v1/settings/embedding -> EmbeddingConfig:
 *        {engine: "local" | "api" | "off", provider, model, local_model,
 *         local: {status, error, model, size_mb, dimension}, indexed_model}
 *        provider/model are the API-engine fields (null unless engine="api");
 *        indexed_model is the identity string of the existing index.
 *   PUT  /api/v1/settings/embedding
 *        {engine, provider?, model?, local_model?, api_key?, base_url?,
 *         credential_name?, force?}
 *        api_key + base_url save a dedicated embedding credential (stored
 *        encrypted under the synthetic provider "embedding:<name>"); with it
 *        the chat credential for the provider is not required. Omitting
 *        api_key keeps the previously saved dedicated credential. Otherwise:
 *        400 when engine="api" and the provider has no credential; 409
 *        (Chinese detail) when the new identity differs from the indexed one —
 *        resend with force: true to confirm dropping and reindexing.
 *   POST /api/v1/settings/embedding/download  {local_model}
 *        -> 202 EmbeddingLocalInfo; starts a background download (idempotent,
 *        repeat calls while downloading return 202 again); 422 for models
 *        outside the whitelist.
 */
import { ApiError, request } from "./client";

export interface ProviderInfo {
  id: string;
  status: string;
}

export interface CredentialInfo {
  id: string;
  provider: string;
  masked_key: string;
}

export interface CredentialInput {
  provider: string;
  api_key: string;
  base_url?: string;
  allow_local?: boolean;
}

export interface SyncModelsResult {
  provider: string;
  count: number;
  models: string[];
}

export type ProbeResult = { provider: string } & Record<string, unknown>;

export interface CustomModelInput {
  provider: string;
  model_id: string;
  display_name?: string;
  capabilities?: Record<string, unknown>;
  // No context_window: the backend resolves the real window itself
  // (verified known-windows table, then catalog history).
}

export interface CustomModelResult {
  provider: string;
  model_id: string;
  display_name: string;
  capabilities: Record<string, unknown>;
}

export function listProviders(): Promise<ProviderInfo[]> {
  return request<ProviderInfo[]>("/api/v1/providers");
}

export function listCredentials(): Promise<CredentialInfo[]> {
  return request<CredentialInfo[]>("/api/v1/credentials");
}

export function saveCredential(input: CredentialInput): Promise<CredentialInfo> {
  return request<CredentialInfo>("/api/v1/credentials", { method: "POST", body: input });
}

/** 204/200 on success; a referenced credential fails with 409 + reason. */
export function deleteCredential(id: string): Promise<void> {
  return request<void>(`/api/v1/credentials/${id}`, { method: "DELETE" });
}

/** Embedding engine for the novel RAG index: bundled local model, provider API, or off. */
export type EmbeddingEngine = "local" | "api" | "off";

export type EmbeddingLocalStatus = "not_downloaded" | "downloading" | "ready" | "error";

export interface EmbeddingLocalInfo {
  status: EmbeddingLocalStatus;
  error: string | null;
  model: string;
  size_mb: number;
  dimension: number;
  /** Download progress 0-1 while status = "downloading"; absent otherwise. */
  progress?: number | null;
}

/** Embedding config for the novel RAG index (GET/PUT /api/v1/settings/embedding). */
export interface EmbeddingConfig {
  engine: EmbeddingEngine;
  /** API-engine fields; null unless engine = "api". */
  provider: string | null;
  model: string | null;
  /** Dedicated embedding credential: synthetic provider row name and its
   *  endpoint (base_url is not secret and is echoed; the key never is). */
  credential_provider: string | null;
  base_url: string | null;
  /** Currently selected local model id. */
  local_model: string;
  /** Status of the bundled local model. */
  local: EmbeddingLocalInfo;
  /** Real on-disk status of every VISIBLE local model (selected but
   *  unsaved models included; hidden registry entries are not exposed). */
  local_models?: Record<string, EmbeddingLocalInfo>;
  /** Visible local models with display metadata — the model picker renders
   *  from this list (no frontend-hardcoded model catalog). */
  visible_models?: EmbeddingVisibleModel[];
  /** Identity string the existing index was built with; null when no index. */
  indexed_model: string | null;
  /** Index reconciliation: chapters that should be indexed vs what the
   *  index actually holds; drift = read side silently returns empty.
   *  rebuilding = a force rebuild is in flight (drift suppressed). */
  index_health?: {
    indexable_chapters: number;
    indexed_documents: number;
    active_chunks: number;
    drift: boolean;
    rebuilding?: boolean;
  };
}

/** A visible local embedding model as offered by the backend registry. */
export interface EmbeddingVisibleModel {
  id: string;
  size_mb: number;
  dimension: number;
  chunk_chars?: number;
}

export interface EmbeddingConfigInput {
  engine: EmbeddingEngine;
  /** Required when engine = "api". */
  provider?: string;
  model?: string;
  /** Dedicated embedding credential (engine = "api"): both required together;
   *  omit api_key to keep the previously saved one. */
  api_key?: string;
  base_url?: string;
  credential_name?: string;
  /** Optional when engine = "local" (backend default otherwise). */
  local_model?: string;
  /** Resend with true to confirm clearing + reindexing after a 409. */
  force?: boolean;
}

export function getEmbeddingConfig(): Promise<EmbeddingConfig> {
  return request<EmbeddingConfig>("/api/v1/settings/embedding");
}

export function putEmbeddingConfig(input: EmbeddingConfigInput): Promise<EmbeddingConfig> {
  return request<EmbeddingConfig>("/api/v1/settings/embedding", { method: "PUT", body: input });
}

/** Start a background download of a whitelisted local model (202, idempotent). */
export function downloadLocalEmbeddingModel(localModel: string): Promise<EmbeddingLocalInfo> {
  return request<EmbeddingLocalInfo>("/api/v1/settings/embedding/download", {
    method: "POST",
    body: { local_model: localModel },
  });
}

// ---------------------------------------------------------------------------
// Cluster mode (GET/PUT /api/v1/settings/cluster, stored in user_preferences)
// ---------------------------------------------------------------------------

/** Per-role model pick: an explicit provider/model pair, or "auto". */
export type ClusterRoleConfig = { provider: string; model: string } | "auto";

export interface ClusterRoles {
  /** Entry role; optional — older configs predate it (treated as "auto"). */
  orchestrator?: ClusterRoleConfig;
  /** Analyst seat; optional — unconfigured it follows the orchestrator. */
  analyst?: ClusterRoleConfig;
  write: ClusterRoleConfig;
  review: ClusterRoleConfig;
  revise: ClusterRoleConfig;
}

/** Reasoning effort tiers (思考强度) offered by the cluster config UI. */
export type ReasoningLevel = "auto" | "none" | "low" | "medium" | "high" | "xhigh" | "max";

/** Per-seat reasoning ceiling: a global default plus per-seat overrides. The
 *  executor downgrades elastically per task type to protect JSON budgets. */
export interface ClusterReasoning {
  default: ReasoningLevel;
  per_role: Partial<Record<keyof ClusterRoles, ReasoningLevel>>;
}

export interface ClusterConfig {
  mode: "normal" | "cluster";
  roles: ClusterRoles;
  reasoning: ClusterReasoning;
  /** Per-seat hint: tiers the seat's configured model serves (null for auto). */
  role_supported_levels?: Partial<Record<keyof ClusterRoles, string[] | null>>;
  /** Models the user can use (has credentials); cluster mode needs >= 2. */
  available_models: number;
}

export function getClusterConfig(): Promise<ClusterConfig | null> {
  // Public build: the cluster settings API is not part of the surface.
  // A 404 reads as "unconfigured"; callers fall back to single-model display.
  return request<ClusterConfig>("/api/v1/settings/cluster").catch((err) => {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  });
}

export function syncModels(providerId: string): Promise<SyncModelsResult> {
  return request<SyncModelsResult>(`/api/v1/providers/${providerId}/sync-models`, { method: "POST" });
}

export function probeProvider(providerId: string): Promise<ProbeResult> {
  return request<ProbeResult>(`/api/v1/providers/${providerId}/probe`, { method: "POST" });
}

/**
 * Manually register a model (POST /api/v1/models). The backend marks it with
 * capabilities.manual = true; used for providers whose model list cannot be
 * auto-discovered (e.g. 火山引擎接入点).
 */
export function addCustomModel(input: CustomModelInput): Promise<CustomModelResult> {
  return request<CustomModelResult>("/api/v1/models", { method: "POST", body: input });
}
