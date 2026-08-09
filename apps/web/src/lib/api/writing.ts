/**
 * Writing-progress + chapter download endpoints —
 * proseforge/api/routes/writing_status.py and the download endpoints in
 * proseforge/api/routes/chapters.py.
 *
 * Confirmed shapes (router prefix /api/v1):
 *   GET /api/v1/projects/{id}/writing-status -> WritingStatus
 *     Per-chapter aggregate from agent_runs/agent_tasks/chapters: status is
 *     one of not_started/writing/reviewing/rewriting/completed/failed, stage
 *     is the Chinese 当前环节描述, downloadable is true only for completed
 *     chapters (COMPLETED run + active version written back).
 *   GET /api/v1/projects/{id}/chapters/download -> zip of every completed
 *     chapter's active-version 正文 (「第N章_标题.md」), 409 when none.
 *   GET /api/v1/projects/{id}/chapters/{cid}/download -> single chapter
 *     markdown, 409 when the chapter has no completed version.
 */
import { ApiError, request } from "./client";

export type WritingChapterStatus =
  | "not_started"
  | "writing"
  | "reviewing"
  | "rewriting"
  | "completed"
  | "failed";

export interface WritingChapter {
  chapter_no: number;
  title: string;
  chapter_id: string | null;
  status: WritingChapterStatus;
  /** 当前环节描述（如「场景起草中」/「意见合并中」/「未开始」）。 */
  stage: string;
  downloadable: boolean;
}

/** 自动暂停信息：executor 连续可重试失败自动暂停时由 writing-status 透传；
 *  人工暂停与健康状态为 null。 */
export interface AutoPauseInfo {
  run_id: string;
  /** run.auto_paused 事件里的错误摘要。 */
  reason: string;
  provider: string;
  model: string;
  streak: number;
}

export interface WritingStatus {
  project_id: string;
  total_chapters: number;
  /** Chapter currently in the pipeline (lowest in-progress 章号), null when idle. */
  current_chapter_no: number | null;
  chapters: WritingChapter[];
  /** 自动暂停信息（null 表示无自动暂停）。 */
  auto_pause?: AutoPauseInfo | null;
  /** 五条流水线状态栏（写作/审校/改写/动态承诺/承诺台账）。 */
  lanes?: WritingLanes;
}

/** 写作/审校/改写栏：active 时 detail 为该章当前环节描述。 */
export interface PipelineLane {
  active: boolean;
  chapter_no: number | null;
  detail: string;
}

/** 奥莉维亚三节点：task 状态原值（PENDING/RUNNING/SUCCEEDED/FAILED），无 run 时全 null。 */
export interface PromisePipelineLane {
  active: boolean;
  chapter_no: number | null;
  contract: string | null;
  verify: string | null;
  register: string | null;
}

export interface PromiseLedger {
  open: number;
  developing: number;
  resolved: number;
}

export interface WritingLanes {
  writing: PipelineLane;
  reviewing: PipelineLane;
  rewriting: PipelineLane;
  promise_pipeline: PromisePipelineLane;
  promise_ledger: PromiseLedger;
}

export function getWritingStatus(projectId: string): Promise<WritingStatus> {
  return request<WritingStatus>(`/api/v1/projects/${projectId}/writing-status`);
}

/** Browser download via a temporary anchor (same-origin cookie auth). */
function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  URL.revokeObjectURL(url);
}

/** GET /api/v1/projects/{id}/chapters/download — every completed chapter as one zip. */
export async function downloadChaptersZip(projectId: string): Promise<void> {
  const response = await fetch(`/api/v1/projects/${projectId}/chapters/download`, { credentials: "include" });
  if (!response.ok) throw new ApiError(response.status, "下载失败，请稍后重试");
  saveBlob(await response.blob(), `proseforge-chapters-${projectId.slice(0, 8)}.zip`);
}

/** GET /api/v1/projects/{id}/chapters/{cid}/download — one completed chapter. */
export async function downloadChapter(projectId: string, chapter: WritingChapter): Promise<void> {
  const response = await fetch(`/api/v1/projects/${projectId}/chapters/${chapter.chapter_id}/download`, {
    credentials: "include",
  });
  if (!response.ok) throw new ApiError(response.status, "下载失败，请稍后重试");
  const safeTitle = chapter.title.replace(/[\\/:*?"<>|]+/g, "_").trim();
  saveBlob(await response.blob(), `第${chapter.chapter_no}章_${safeTitle || `第${chapter.chapter_no}章`}.md`);
}
