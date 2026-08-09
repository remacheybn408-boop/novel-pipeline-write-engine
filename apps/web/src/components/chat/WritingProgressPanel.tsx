import { useEffect, useState, type ReactNode } from "react";
import { controlAgentRun } from "../../lib/api/agentRuns";
import {
  downloadChapter,
  downloadChaptersZip,
  getWritingStatus,
  type AutoPauseInfo,
  type PromisePipelineLane,
  type WritingChapter,
  type WritingChapterStatus,
  type WritingLanes,
  type WritingStatus,
} from "../../lib/api/writing";
import { DownloadIcon } from "../ui/icons";

/** Badge per chapter status: 未开始灰 / 写作中蓝 / 审校中黄 / 改写中紫 / 完成绿 / 失败红. */
export const CHAPTER_BADGES: Record<WritingChapterStatus, { label: string; className: string }> = {
  not_started: { label: "未开始", className: "bg-hover/60 text-ink-secondary" },
  writing: { label: "写作中", className: "bg-blue-100 text-blue-700" },
  reviewing: { label: "审校中", className: "bg-amber-100 text-amber-700" },
  rewriting: { label: "改写中", className: "bg-violet-100 text-violet-700" },
  completed: { label: "完成", className: "bg-emerald-100 text-emerald-700" },
  failed: { label: "失败", className: "bg-red-100 text-red-600" },
};

export function chapterBadge(status: WritingChapterStatus): { label: string; className: string } {
  return CHAPTER_BADGES[status] ?? CHAPTER_BADGES.not_started;
}

/** A chapter row's download button is enabled only for a completed chapter
 *  with a backing chapter row (plan-only chapters have nothing to export). */
export function canDownloadChapter(chapter: WritingChapter): boolean {
  return chapter.status === "completed" && chapter.downloadable && Boolean(chapter.chapter_id);
}

export function downloadableChapters(status: WritingStatus): WritingChapter[] {
  return status.chapters.filter(canDownloadChapter);
}

const cardClass = "rounded-2xl border border-line bg-white/60 p-4 backdrop-blur-sm";
const POLL_INTERVAL_MS = 10_000;

/** 动态承诺节点状态徽标：SUCCEEDED✓ / RUNNING…中 / PENDING待 / FAILED✗。 */
export function promiseNodeBadge(status: string | null): { text: string; className: string } {
  switch (status) {
    case "SUCCEEDED":
      return { text: "✓", className: "text-emerald-600" };
    case "RUNNING":
      return { text: "…中", className: "text-blue-600" };
    case "PENDING":
      return { text: "待", className: "text-ink-secondary" };
    case "FAILED":
      return { text: "✗", className: "text-red-600" };
    default:
      return { text: "—", className: "text-ink-secondary" };
  }
}

function PromisePipelineBrief({ lane }: { lane: PromisePipelineLane }) {
  const nodes: Array<[string, string | null]> = [
    ["契约", lane.contract],
    ["验证", lane.verify],
    ["登记", lane.register],
  ];
  return (
    <span className="text-ink-secondary">
      {lane.chapter_no !== null && `第${lane.chapter_no}章 `}
      {nodes.map(([label, status], index) => {
        const badge = promiseNodeBadge(status);
        return (
          <span key={label}>
            {index > 0 && " "}
            {label}
            <span className={badge.className}>{badge.text}</span>
          </span>
        );
      })}
    </span>
  );
}

/** 五条流水线状态栏：写作/审校/改写/动态承诺/承诺台账。 */
export function PipelineLanesCard({ lanes }: { lanes: WritingLanes }) {
  const rows: Array<{ name: string; active: boolean; brief: ReactNode }> = [
    {
      name: "写作",
      active: lanes.writing.active,
      brief: lanes.writing.active ? `第${lanes.writing.chapter_no}章 · ${lanes.writing.detail}` : "空闲",
    },
    {
      name: "审校",
      active: lanes.reviewing.active,
      brief: lanes.reviewing.active ? `第${lanes.reviewing.chapter_no}章 · ${lanes.reviewing.detail}` : "空闲",
    },
    {
      name: "改写",
      active: lanes.rewriting.active,
      brief: lanes.rewriting.active ? `第${lanes.rewriting.chapter_no}章 · ${lanes.rewriting.detail}` : "空闲",
    },
    {
      name: "动态承诺",
      active: lanes.promise_pipeline.active,
      brief: <PromisePipelineBrief lane={lanes.promise_pipeline} />,
    },
    {
      name: "承诺",
      active: lanes.promise_ledger.open + lanes.promise_ledger.developing > 0,
      brief: `未结 ${lanes.promise_ledger.open} · 推进 ${lanes.promise_ledger.developing} · 兑现 ${lanes.promise_ledger.resolved}`,
    },
  ];
  return (
    <section className={cardClass} aria-label="流水线状态">
      <h2 className="text-sm font-semibold text-ink">流水线状态</h2>
      <ul className="mt-2 flex flex-col gap-1.5">
        {rows.map((row) => (
          <li key={row.name} className="flex items-center gap-2 text-xs">
            <span
              className={`h-2 w-2 shrink-0 rounded-full ${row.active ? "animate-pulse bg-emerald-500" : "bg-ink-secondary/30"}`}
              aria-hidden="true"
            />
            <span className="w-14 shrink-0 font-medium text-ink">{row.name}</span>
            <span className="min-w-0 flex-1 truncate text-ink-secondary">{row.brief}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** 自动暂停提示条 + 手动恢复按钮：调 POST /agent-runs/{id}/resume，
 *  成功后由 onResumed 触发一次即时刷新（轮询本身不中断）。 */
export function AutoPauseBanner({ info, onResumed }: { info: AutoPauseInfo; onResumed: () => void }) {
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);

  async function handleResume() {
    setResuming(true);
    setResumeError(null);
    try {
      await controlAgentRun(info.run_id, "resume");
      onResumed();
    } catch {
      setResumeError("恢复失败，请稍后重试");
    } finally {
      setResuming(false);
    }
  }

  return (
    <section className={cardClass} aria-label="自动暂停">
      <p className="text-xs text-amber-700">
        模型访问不稳定已暂停（{info.provider}/{info.model}）
      </p>
      <button
        type="button"
        disabled={resuming}
        onClick={() => void handleResume()}
        className="mt-2 flex w-full items-center justify-center rounded-xl border border-line bg-white/80 px-3 py-1.5 text-xs text-ink transition-colors hover:bg-white disabled:cursor-not-allowed disabled:opacity-40"
      >
        {resuming ? "恢复中…" : "恢复"}
      </button>
      {resumeError && <p className="mt-1.5 text-xs text-red-600">{resumeError}</p>}
    </section>
  );
}

/**
 * Per-chapter writing progress for the work-mode chat page, docked above the
 * SwarmWorkbench sidebar. Polls writing-status every 10s; transient poll
 * failures keep the last snapshot (a blip must not blank the panel). Hidden
 * until the project has any chapter (empty projects show nothing).
 */
export function WritingProgressPanel({ projectId }: { projectId: string }) {
  const [status, setStatus] = useState<WritingStatus | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  // Bump to force an immediate refetch (manual resume from the pause banner).
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    let stopped = false;
    let timer = 0;
    async function tick() {
      try {
        const data = await getWritingStatus(projectId);
        if (!stopped) setStatus(data);
      } catch {
        // Keep the last snapshot through transient failures.
      }
      if (!stopped) timer = window.setTimeout(() => void tick(), POLL_INTERVAL_MS);
    }
    void tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [projectId, refreshTick]);

  if (!status || status.total_chapters === 0) return null;

  const completed = downloadableChapters(status);
  const currentChapter =
    status.current_chapter_no === null
      ? null
      : status.chapters.find((chapter) => chapter.chapter_no === status.current_chapter_no) ?? null;

  async function handleDownloadAll() {
    setDownloadError(null);
    try {
      await downloadChaptersZip(projectId);
    } catch {
      setDownloadError("下载失败，请稍后重试");
    }
  }

  async function handleDownloadOne(chapter: WritingChapter) {
    setDownloadError(null);
    try {
      await downloadChapter(projectId, chapter);
    } catch {
      setDownloadError("下载失败，请稍后重试");
    }
  }

  return (
    <aside className="flex shrink-0 flex-col gap-3 px-4 pt-6">
      {status.auto_pause && (
        <AutoPauseBanner info={status.auto_pause} onResumed={() => setRefreshTick((tick) => tick + 1)} />
      )}
      {status.lanes && <PipelineLanesCard lanes={status.lanes} />}
      <section className={cardClass}>
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-ink">写作进度</h2>
          <span className="shrink-0 text-[11px] text-ink-secondary">
            {completed.length}/{status.total_chapters} 章完成
          </span>
        </div>
        {currentChapter && (
          <p className="mt-1.5 text-xs text-ink-secondary">
            当前：第 {currentChapter.chapter_no} 章 · {currentChapter.stage}
          </p>
        )}
        <ul className="mt-2 flex max-h-56 flex-col gap-1.5 overflow-y-auto">
          {status.chapters.map((chapter) => {
            const badge = chapterBadge(chapter.status);
            const downloadable = canDownloadChapter(chapter);
            return (
              <li key={chapter.chapter_no} className="flex items-center gap-2 text-xs">
                <span className="min-w-0 flex-1 truncate text-ink-secondary" title={chapter.title}>
                  第{chapter.chapter_no}章 {chapter.title}
                </span>
                <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${badge.className}`}>
                  {badge.label}
                </span>
                <button
                  type="button"
                  title={downloadable ? "下载本章" : "章节完成后可下载"}
                  aria-label={`下载第${chapter.chapter_no}章`}
                  disabled={!downloadable}
                  onClick={() => void handleDownloadOne(chapter)}
                  className="shrink-0 text-ink-secondary transition-colors hover:text-ink disabled:cursor-not-allowed disabled:opacity-30"
                >
                  <DownloadIcon size={14} />
                </button>
              </li>
            );
          })}
        </ul>
        <button
          type="button"
          disabled={completed.length === 0}
          onClick={() => void handleDownloadAll()}
          className="mt-3 flex w-full items-center justify-center gap-1.5 rounded-xl border border-line bg-white/80 px-3 py-1.5 text-xs text-ink transition-colors hover:bg-white disabled:cursor-not-allowed disabled:opacity-40"
        >
          <DownloadIcon size={14} />
          下载全部已完成（{completed.length}）
        </button>
        {downloadError && <p className="mt-2 text-xs text-red-600">{downloadError}</p>}
      </section>
    </aside>
  );
}
