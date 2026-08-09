/**
 * Setting-conflict endpoints — heuristic consistency checks between new
 * chapters and existing settings (work-mode projects only).
 *
 * Confirmed shapes:
 *   GET  /api/v1/projects/{project_id}/conflicts?status=open -> StoryConflict[]
 *   POST /api/v1/projects/{project_id}/conflicts/{conflict_id}/resolve
 *        {resolution?} -> the row turns resolved
 *
 * Heuristic detection: false positives are expected. The UI prompts for
 * human review and never auto-modifies settings.
 */
import { request } from "./client";

export interface StoryConflict {
  id: string;
  /** Human-readable claim, e.g. "顾长风 · 身份". */
  field_or_claim: string;
  /** e.g. "chapter_version:<id>". */
  candidate_source: string;
  /** e.g. "character:<id>" | "story_bible:<id>". */
  conflicting_source: string;
  /** JSON string: {candidate_value, existing_value, chapter_no}. */
  evidence_json: string;
  status: "open" | "resolved";
  created_at: string;
}

/** Parsed shape of StoryConflict.evidence_json (all fields best-effort). */
export interface ConflictEvidence {
  candidate_value?: string;
  existing_value?: string;
  chapter_no?: number;
}

export function listConflicts(projectId: string, status: "open" | "resolved" = "open"): Promise<StoryConflict[]> {
  return request<StoryConflict[]>(`/api/v1/projects/${projectId}/conflicts`, { query: { status } });
}

export function resolveConflict(projectId: string, conflictId: string, resolution?: string): Promise<void> {
  return request<void>(`/api/v1/projects/${projectId}/conflicts/${conflictId}/resolve`, {
    method: "POST",
    body: resolution ? { resolution } : {},
  });
}
