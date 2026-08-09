/**
 * Project endpoints — proseforge/api/routes/projects.py
 *
 * Confirmed shapes:
 *   GET  /api/v1/projects?mode=work|chat  -> Project[] (mode filter, optional)
 *   POST /api/v1/projects      {slug, title, genre?, style?, mode?} -> 201 Project
 *                              slug must match ^[a-z0-9][a-z0-9-]*$, 409 on conflict.
 */
import { ApiError, request } from "./client";
import type { ClusterReasoning, ClusterRoles } from "./settings";

export type ProjectMode = "work" | "chat";

export interface Project {
  id: string;
  slug: string;
  title: string;
  genre: string;
  style: string;
  language: string;
  status: string;
  mode: string;
  /**
   * Writing-model lock (read-only): set once an outline is imported or the
   * first chapter starts — whichever comes first. All null while unlocked.
   */
  writing_model_provider?: string | null;
  writing_model_id?: string | null;
  model_locked_at?: string | null;
  model_lock_source?: "outline_import" | "first_chapter" | null;
}

export interface ProjectCreateInput {
  slug: string;
  title: string;
  genre?: string;
  style?: string;
  mode?: ProjectMode;
}

/** Derive a backend-valid slug (^[a-z0-9][a-z0-9-]*$) from a free-form title. */
export function slugify(title: string): string {
  const slug = title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || `p-${Date.now().toString(36)}`;
}

export function listProjects(mode?: ProjectMode, archived?: boolean): Promise<Project[]> {
  return request<Project[]>("/api/v1/projects", { query: { mode, archived } });
}

export function createProject(input: ProjectCreateInput): Promise<Project> {
  return request<Project>("/api/v1/projects", { method: "POST", body: input });
}

/** POST /api/v1/projects/{id}/archive -> status "ARCHIVED" (no body). */
export function archiveProject(id: string): Promise<void> {
  return request<void>(`/api/v1/projects/${id}/archive`, { method: "POST" });
}

/** POST /api/v1/projects/{id}/restore -> status "ACTIVE" (no body). */
export function restoreProject(id: string): Promise<void> {
  return request<void>(`/api/v1/projects/${id}/restore`, { method: "POST" });
}

export function updateProject(projectId: string, input: { title: string }): Promise<Project> {
  return request<Project>(`/api/v1/projects/${projectId}`, { method: "PATCH", body: input });
}

export function deleteProject(projectId: string): Promise<void> {
  return request<void>(`/api/v1/projects/${projectId}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Per-project cluster override — GET /api/v1/projects/{project_id}/cluster-config
// (read-only in the public build; the swarm executor resolves stored configs)
// ---------------------------------------------------------------------------

export interface ProjectClusterConfig {
  /** Effective chain: project override, global default, or unconfigured. */
  source: "project" | "global" | "none";
  /** True when the project carries its own config. */
  override: boolean;
  mode: "normal" | "cluster";
  roles: ClusterRoles;
  /** Per-seat reasoning ceiling; absent on responses from older backends. */
  reasoning?: ClusterReasoning;
  /** Models the user can use (has credentials); cluster mode needs >= 2. */
  available_models: number;
}

export function getProjectClusterConfig(projectId: string): Promise<ProjectClusterConfig | null> {
  // Public build: the per-project cluster API is not part of the surface.
  // A 404 reads as "unconfigured"; callers fall back to single-model display.
  return request<ProjectClusterConfig>(`/api/v1/projects/${projectId}/cluster-config`).catch((err) => {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  });
}

// ---------------------------------------------------------------------------
// Chapter content — GET /api/v1/projects/{id}/chapters/{cid}/content
// ---------------------------------------------------------------------------

export interface ChapterContent {
  chapter_id: string;
  title: string;
  chapter_no: number;
  content: string;
}

export function getChapterContent(projectId: string, chapterId: string): Promise<ChapterContent> {
  return request<ChapterContent>(`/api/v1/projects/${projectId}/chapters/${chapterId}/content`);
}
