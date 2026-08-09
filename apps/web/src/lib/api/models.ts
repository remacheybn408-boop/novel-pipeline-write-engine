/**
 * Model catalog endpoint — proseforge/api/routes/providers.py
 *
 * Confirmed shape:
 *   GET /api/v1/models -> ModelInfo[]
 *   Optional query params: provider, q, available_only (default true).
 */
import { request } from "./client";

export interface ModelInfo {
  provider: string;
  model_id: string;
  display_name: string;
  capabilities: Record<string, unknown>;
  context_window: number | null;
  max_output_tokens: number | null;
  /** Supported reasoning levels; models without a profile only get ["auto"]. */
  reasoning_levels?: string[];
  /**
   * Owner of a manual row (null = synced row or legacy shared manual row).
   * Delete is allowed only when capabilities.manual && owner_id != null.
   */
  owner_id?: string | null;
  /** True on pre-ownership manual rows: visible to everyone, undeletable. */
  legacy_shared?: boolean;
}

export function listModels(params?: {
  provider?: string;
  q?: string;
  available_only?: boolean;
}): Promise<ModelInfo[]> {
  return request<ModelInfo[]>("/api/v1/models", { query: { ...params } });
}

/**
 * Delete a manually registered model owned by the current user
 * (DELETE /api/v1/models/{provider}/{model_id}). Backend answers 403 for
 * synced / legacy shared rows and 404 for missing or foreign-owned rows.
 */
export function deleteModel(provider: string, modelId: string): Promise<void> {
  return request<void>(`/api/v1/models/${encodeURIComponent(provider)}/${encodeURIComponent(modelId)}`, {
    method: "DELETE",
  });
}

// Conversation/chat endpoints live in ./conversations.ts
