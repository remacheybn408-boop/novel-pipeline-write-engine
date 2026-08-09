/**
 * Plugin endpoints (skills + MCP servers) — backend under parallel
 * development; call shapes per the agreed contract:
 *
 *   GET    /api/v1/plugins/skills            -> Skill[]
 *   POST   /api/v1/plugins/skills            {name, description?, content, enabled?} -> 201 Skill
 *   PATCH  /api/v1/plugins/skills/{id}       {name?, description?, content?, enabled?} -> Skill
 *   DELETE /api/v1/plugins/skills/{id}       -> 204
 *   POST   /api/v1/plugins/skills/upload     multipart field "file" (.md/.zip) -> 201 Skill
 *   GET    /api/v1/plugins/mcp-servers       -> McpServer[]
 *   POST   /api/v1/plugins/mcp-servers       {name, transport, url, headers?, enabled?} -> 201
 *   PATCH  /api/v1/plugins/mcp-servers/{id}  same fields, all optional
 *   DELETE /api/v1/plugins/mcp-servers/{id}  -> 204
 *   POST   /api/v1/plugins/mcp-servers/{id}/probe
 *          -> {ok: true, tools: string[]} | {ok: false, error: string}
 */
import { ApiError, request } from "./client";

export interface Skill {
  /** "builtin:<skill_key>" for built-ins, a uuid for user-created skills. */
  id: string;
  /** Present only on built-in skills. */
  skill_key: string | null;
  name: string;
  description: string;
  content: string;
  enabled: boolean;
  builtin: boolean;
  /** Built-in grouping: "fiction" (小说类 genre packs) or "tool" (工具类);
   *  null on user-created skills and pre-category backends. */
  category: "fiction" | "tool" | null;
  /** Null for built-in skills. */
  created_at: string | null;
}

export interface SkillInput {
  name: string;
  description?: string;
  content: string;
  enabled?: boolean;
}

export type SkillPatch = Partial<SkillInput>;

export type McpTransport = "streamable-http" | "sse";

export interface McpServer {
  id: string;
  name: string;
  transport: McpTransport;
  url: string;
  enabled: boolean;
  created_at: string;
  header_keys: string[];
}

export interface McpServerInput {
  name: string;
  transport: McpTransport;
  url: string;
  headers?: Record<string, string>;
  enabled?: boolean;
}

export type McpServerPatch = Partial<McpServerInput>;

export type McpProbeResult = { ok: true; tools: string[] } | { ok: false; error: string };

export function listSkills(): Promise<Skill[]> {
  return request<Skill[]>("/api/v1/plugins/skills");
}

export function createSkill(input: SkillInput): Promise<Skill> {
  return request<Skill>("/api/v1/plugins/skills", { method: "POST", body: input });
}

export function updateSkill(id: string, patch: SkillPatch): Promise<Skill> {
  return request<Skill>(`/api/v1/plugins/skills/${id}`, { method: "PATCH", body: patch });
}

/**
 * Toggle a built-in skill — PATCH /api/v1/plugins/skills/builtin/{skill_key}
 * with body {enabled}. Built-ins support enable/disable only (no delete).
 */
export function setBuiltinSkillEnabled(skillKey: string, enabled: boolean): Promise<Skill> {
  return request<Skill>(`/api/v1/plugins/skills/builtin/${skillKey}`, {
    method: "PATCH",
    body: { enabled },
  });
}

export function deleteSkill(id: string): Promise<void> {
  return request<void>(`/api/v1/plugins/skills/${id}`, { method: "DELETE" });
}

/**
 * Upload a skill file (.md or .zip) as multipart form data. The shared
 * request() wrapper is JSON-only, so this uses fetch directly (still
 * same-origin with the session cookie).
 */
export async function uploadSkill(file: File): Promise<Skill> {
  const form = new FormData();
  form.append("file", file);

  let response: Response;
  try {
    response = await fetch("/api/v1/plugins/skills/upload", {
      method: "POST",
      credentials: "include",
      body: form,
    });
  } catch {
    throw new ApiError(0, "网络连接失败，请确认后端服务已启动");
  }

  if (!response.ok) {
    let detail: string | null = null;
    try {
      const payload: unknown = await response.json();
      if (payload && typeof payload === "object" && "detail" in payload) {
        const raw = (payload as { detail: unknown }).detail;
        detail = typeof raw === "string" ? raw : JSON.stringify(raw);
      }
    } catch {
      // Non-JSON error body; use the generic message below.
    }
    throw new ApiError(response.status, detail ?? `上传失败（HTTP ${response.status}）`);
  }

  return (await response.json()) as Skill;
}

export function listMcpServers(): Promise<McpServer[]> {
  return request<McpServer[]>("/api/v1/plugins/mcp-servers");
}

export function createMcpServer(input: McpServerInput): Promise<McpServer> {
  return request<McpServer>("/api/v1/plugins/mcp-servers", { method: "POST", body: input });
}

export function updateMcpServer(id: string, patch: McpServerPatch): Promise<McpServer> {
  return request<McpServer>(`/api/v1/plugins/mcp-servers/${id}`, { method: "PATCH", body: patch });
}

export function deleteMcpServer(id: string): Promise<void> {
  return request<void>(`/api/v1/plugins/mcp-servers/${id}`, { method: "DELETE" });
}

export function probeMcpServer(id: string): Promise<McpProbeResult> {
  return request<McpProbeResult>(`/api/v1/plugins/mcp-servers/${id}/probe`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Built-in tools (per-user toggles)
// ---------------------------------------------------------------------------

export interface WebSearchToolState {
  enabled: boolean;
}

/**
 * GET/PATCH /api/v1/plugins/tools/web-search — per-user switch for the
 * built-in web search tool (work mode only; no API key required).
 */
export function getWebSearchTool(): Promise<WebSearchToolState> {
  return request<WebSearchToolState>("/api/v1/plugins/tools/web-search");
}

export function setWebSearchToolEnabled(enabled: boolean): Promise<WebSearchToolState> {
  return request<WebSearchToolState>("/api/v1/plugins/tools/web-search", {
    method: "PATCH",
    body: { enabled },
  });
}

export interface WebReaderToolState {
  enabled: boolean;
}

/**
 * GET/PATCH /api/v1/plugins/tools/web-reader — per-user switch for the
 * built-in web reader tool (read page bodies, extract links; pairs with
 * web search).
 */
export function getWebReaderTool(): Promise<WebReaderToolState> {
  return request<WebReaderToolState>("/api/v1/plugins/tools/web-reader");
}

export function setWebReaderToolEnabled(enabled: boolean): Promise<WebReaderToolState> {
  return request<WebReaderToolState>("/api/v1/plugins/tools/web-reader", {
    method: "PATCH",
    body: { enabled },
  });
}

export interface DocReaderToolState {
  enabled: boolean;
}

/**
 * GET/PATCH /api/v1/plugins/tools/doc-reader — per-user switch for the
 * built-in document reader tool (toggle_key builtin-doc-reader): lets the
 * model read PDF / DOCX / CSV / XLSX documents behind a URL.
 */
export function getDocReaderTool(): Promise<DocReaderToolState> {
  return request<DocReaderToolState>("/api/v1/plugins/tools/doc-reader");
}

export function setDocReaderToolEnabled(enabled: boolean): Promise<DocReaderToolState> {
  return request<DocReaderToolState>("/api/v1/plugins/tools/doc-reader", {
    method: "PATCH",
    body: { enabled },
  });
}

export interface CodeRunnerToolState {
  enabled: boolean;
}

/**
 * GET/PATCH /api/v1/plugins/tools/code-runner — per-user switch for the
 * built-in code runner tool (toggle_key builtin-code-runner): lets the model
 * run Python (pandas/numpy/matplotlib) in an isolated sandbox; generated
 * charts and files come back as downloadable attachments.
 */
export function getCodeRunnerTool(): Promise<CodeRunnerToolState> {
  return request<CodeRunnerToolState>("/api/v1/plugins/tools/code-runner");
}

export function setCodeRunnerToolEnabled(enabled: boolean): Promise<CodeRunnerToolState> {
  return request<CodeRunnerToolState>("/api/v1/plugins/tools/code-runner", {
    method: "PATCH",
    body: { enabled },
  });
}
