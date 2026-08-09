/**
 * Agent run ("集群模式") endpoints — proseforge/api/routes/agent_runs.py.
 *
 * Confirmed shapes (router prefix /api/v3):
 *   POST /api/v3/projects/{project_id}/agent-runs
 *     body {goal} (+ optional chapter_id/base_version_id/fault_mode/budget_limit/
 *     graph_revision/tasks), header "Idempotency-Key" (optional, echoed for
 *     dedupe). -> 201 run response. Default graph: chief_planner +
 *     continuity_reviewer (depends_on planner).
 *   GET  /api/v3/agent-runs/{id}          -> AgentRun
 *   GET  /api/v3/agent-runs/{id}/tasks    -> AgentTask[]
 *   GET  /api/v3/agent-runs/{id}/artifacts -> AgentArtifact[]
 *   GET  /api/v3/agent-runs/{id}/reviews  -> AgentReview[]
 *   POST /api/v3/agent-runs/{id}/{pause|resume|cancel|retry} -> AgentRun
 *     Transitions: pause PENDING/RUNNING->PAUSED; resume PAUSED->RUNNING
 *     (FAILED is retry-only, 409 otherwise); cancel PENDING/RUNNING/PAUSED/FAILED/
 *     BUDGET_EXHAUSTED->CANCELLED; retry FAILED/PAUSED/BUDGET_EXHAUSTED->RUNNING
 *     (RUNNING is not retryable — 409 RUN_NOT_RETRYABLE). resume/retry re-check
 *     the per-user concurrency cap — 409 RUN_CONCURRENCY_LIMIT when full.
 *   Errors are {"error": {code, message, retryable, request_id, details}}
 *   envelopes (handled by the shared request wrapper) or FastAPI {"detail"}.
 */
import { uuid } from "../uuid";
import { ApiError, request } from "./client";

export type AgentRunStatus = "PENDING" | "RUNNING" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED" | "BUDGET_EXHAUSTED";

/** Statuses in which polling should stop. */
export const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set(["COMPLETED", "FAILED", "CANCELLED", "BUDGET_EXHAUSTED"]);

export interface AgentRun {
  id: string;
  project_id: string;
  status: AgentRunStatus;
  goal_hash: string;
  graph_revision: number;
  checkpoint_id: string | null;
  budget_used: number;
  budget_limit: number | null;
  event_cursor: number;
  policy_version: number;
  terminal_reason: string | null;
  chapter_id: string | null;
  base_version_id: string | null;
  proposal_id: string | null;
  fault_mode: string | null;
  /** Review gate: true = passed (no rewrite), false = auto-rewritten, null = not evaluated yet. */
  gate?: boolean | null;
}

export type AgentTaskStatus = "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "SKIPPED";

export interface AgentTask {
  id: string;
  task_key: string;
  role: string;
  status: AgentTaskStatus;
  attempts: number;
  token_budget: number | null;
  depends_on: string[];
  /** Cluster lane the task belongs to (swarm cluster mode). */
  cluster_role?: "write" | "review" | "revise" | null;
  /** Last failure detail, when the task errored. */
  last_error?: string | null;
}

export interface AgentArtifact {
  id: string;
  artifact_type: string;
  sha256: string;
  preview: string;
  provenance: Record<string, unknown>;
}

export type ReviewStatus = "PASS" | "WARNING" | "CONFLICT" | "UNSUPPORTED";

export interface AgentReview {
  id: string;
  artifact_id: string;
  reviewer_role: string;
  status: ReviewStatus;
  evidence: Record<string, unknown>[];
  conflict_group: string | null;
}

export function createAgentRun(
  projectId: string,
  goal: string,
  model?: { provider: string; model_id: string } | null,
): Promise<AgentRun> {
  return request<AgentRun>(`/api/v3/projects/${projectId}/agent-runs`, {
    method: "POST",
    body: { goal, ...(model ? { provider: model.provider, model: model.model_id } : {}) },
    headers: { "Idempotency-Key": uuid() },
  });
}

export function getAgentRun(runId: string): Promise<AgentRun> {
  return request<AgentRun>(`/api/v3/agent-runs/${runId}`);
}

export function listAgentRunTasks(runId: string): Promise<AgentTask[]> {
  return request<AgentTask[]>(`/api/v3/agent-runs/${runId}/tasks`);
}

export function listAgentRunArtifacts(runId: string): Promise<AgentArtifact[]> {
  return request<AgentArtifact[]>(`/api/v3/agent-runs/${runId}/artifacts`);
}

export function listAgentRunReviews(runId: string): Promise<AgentReview[]> {
  return request<AgentReview[]>(`/api/v3/agent-runs/${runId}/reviews`);
}

export type RunControlAction = "pause" | "resume" | "cancel" | "retry";

export function controlAgentRun(runId: string, action: RunControlAction): Promise<AgentRun> {
  return request<AgentRun>(`/api/v3/agent-runs/${runId}/${action}`, { method: "POST" });
}

/**
 * GET /api/v3/agent-runs/{id}/export.zip — triggers a browser download.
 * Same-origin cookie auth (credentials: "include", same as client.ts).
 */
export async function exportRunZip(runId: string): Promise<void> {
  const response = await fetch(`/api/v3/agent-runs/${runId}/export.zip`, { credentials: "include" });
  if (!response.ok) throw new ApiError(response.status, "导出失败，请稍后重试");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `agent-run-${runId}.zip`;
  anchor.click();
  URL.revokeObjectURL(url);
}
