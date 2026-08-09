/**
 * Tool usage metrics — GET /api/v1/tools/metrics?days=1|7|30
 * (backend under parallel development; call shape per the agreed contract).
 *
 * Empty windows report zeroed rates, empty arrays, and null p50/p95.
 */
import { request } from "./client";

export type ToolMetricsDays = 1 | 7 | 30;

export interface ToolMetricsToolRow {
  tool_name: string;
  calls: number;
  ok: number;
  failed: number;
  success_rate: number;
  timeouts: number;
  cache_hits: number;
  cache_hit_rate: number;
  p50_ms: number | null;
  p95_ms: number | null;
  /** error_class -> occurrence count. */
  errors: Record<string, number>;
}

export interface ToolRecentFailure {
  tool_name: string;
  error_class: string;
  result_summary: string;
  created_at: string;
}

export interface ToolMetrics {
  window_days: number;
  since: string;
  total_calls: number;
  success_rate: number;
  timeout_rate: number;
  cache_hit_rate: number;
  tools: ToolMetricsToolRow[];
  recent_failures: ToolRecentFailure[];
}

export function getToolMetrics(days: ToolMetricsDays): Promise<ToolMetrics> {
  return request<ToolMetrics>(`/api/v1/tools/metrics?days=${days}`);
}
