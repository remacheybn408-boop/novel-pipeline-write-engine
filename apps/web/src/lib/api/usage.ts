/**
 * Usage records endpoint — proseforge/api/routes/usage.py
 *
 * Confirmed shape:
 *   GET /api/v1/usage/records?conversation_id=<id> -> UsageRecord[]
 *   (also accepts project_id / workflow_id / message_id / limit=1..500)
 * Token caching note: providers like deepseek do server-side context
 * caching; hits show up in cached_input_tokens.
 */
import { request } from "./client";

export interface UsageRecord {
  id: string;
  user_id: string;
  project_id: string | null;
  conversation_id: string | null;
  message_id: string | null;
  workflow_run_id: string | null;
  workflow_step: string | null;
  provider: string;
  model_id: string;
  provider_request_id: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  reasoning_tokens: number | null;
  total_tokens: number | null;
  cost_usd: number | null;
  usage_source: string;
  is_final: boolean;
  latency_ms: number | null;
  metadata: Record<string, unknown>;
}

export function listUsageRecords(conversationId: string): Promise<UsageRecord[]> {
  return request<UsageRecord[]>("/api/v1/usage/records", {
    query: { conversation_id: conversationId },
  });
}

/**
 * Per-model token usage — GET /api/v1/usage/by-model?days=1|7|30
 * (proseforge/api/routes/usage.py). Rows are grouped by provider+model_id
 * and sorted by total_tokens desc; totals roll every model up.
 */
export type ModelUsageDays = 1 | 7 | 30;

export interface ModelUsageRow {
  provider: string;
  model_id: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  avg_latency_ms: number | null;
  last_used_at: string;
}

export interface ModelUsageTotals {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  /** Call-weighted average across models; null when no latency was recorded. */
  avg_latency_ms: number | null;
}

export interface ModelUsage {
  days: number;
  rows: ModelUsageRow[];
  totals: ModelUsageTotals;
}

export function getModelUsage(days: ModelUsageDays): Promise<ModelUsage> {
  return request<ModelUsage>(`/api/v1/usage/by-model?days=${days}`);
}
