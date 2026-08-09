import { useEffect, useRef, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import {
  TERMINAL_RUN_STATUSES,
  controlAgentRun,
  getAgentRun,
  listAgentRunArtifacts,
  listAgentRunReviews,
  listAgentRunTasks,
  type AgentArtifact,
  type AgentReview,
  type AgentRun,
  type AgentTask,
  type RunControlAction,
} from "../../lib/api/agentRuns";
import { ApiError } from "../../lib/api/client";
import { RUN_STATUS_LABELS, roleLabel, taskLabel } from "../../lib/agentRoles";
import { GripVerticalIcon } from "../../components/ui/icons";

/** Passed via navigate() state right after starting the run. */
interface NavigationState {
  goal?: string;
}

export const TASK_STATUS_LABELS: Record<string, string> = {
  PENDING: "待办",
  RUNNING: "进行中",
  SUCCEEDED: "完成",
  FAILED: "失败",
  SKIPPED: "跳过",
};

/** Run statuses whose reviews are worth fetching (a failed run still has a verdict). */
export const REVIEW_FETCH_STATUSES: ReadonlySet<string> = new Set(["COMPLETED", "FAILED"]);

const REVIEW_STATUS_LABELS: Record<string, string> = {
  PASS: "通过",
  WARNING: "警告",
  CONFLICT: "冲突",
  UNSUPPORTED: "证据不足",
};

function RunStatusBadge({ status }: { status: string }) {
  const tone =
    status === "FAILED"
      ? "bg-red-50 text-red-600"
      : status === "COMPLETED"
        ? "bg-emerald-50 text-emerald-700"
        : "bg-hover text-ink";
  return <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${tone}`}>{RUN_STATUS_LABELS[status] ?? status}</span>;
}

const controlButtonClass =
  "rounded-lg border border-line bg-white px-3 py-1.5 text-xs text-ink transition-colors hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50";

/** Which control buttons make sense for each run status (mirrors backend transitions). */
function controlsFor(status: string): { action: RunControlAction; label: string }[] {
  switch (status) {
    case "PENDING":
    case "RUNNING":
      return [
        { action: "pause", label: "暂停" },
        { action: "cancel", label: "取消" },
      ];
    case "PAUSED":
      return [
        { action: "resume", label: "继续" },
        { action: "cancel", label: "取消" },
      ];
    case "FAILED":
      return [{ action: "retry", label: "重试" }];
    case "BUDGET_EXHAUSTED":
      // Backend allows retry and cancel for budget-exhausted runs.
      return [
        { action: "retry", label: "重试" },
        { action: "cancel", label: "取消" },
      ];
    default:
      return [];
  }
}

function TaskCard({ task }: { task: AgentTask }) {
  const running = task.status === "RUNNING";
  return (
    <div
      className={`flex items-center gap-3 rounded-2xl border border-line bg-white px-5 py-4 ${
        running ? "animate-pulse" : ""
      }`}
    >
      <GripVerticalIcon size={16} className="shrink-0 text-ink-secondary/60" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-ink">{taskLabel(task)}</p>
        <p className="truncate text-xs text-ink-secondary">
          {task.task_key}
          {task.attempts > 1 && ` · 已重试 ${task.attempts - 1} 次`}
        </p>
      </div>
      <span
        className={`shrink-0 rounded-full px-2.5 py-1 text-xs ${
          task.status === "FAILED" ? "bg-red-50 text-red-600" : "bg-hover text-ink-secondary"
        }`}
      >
        {TASK_STATUS_LABELS[task.status] ?? task.status}
      </span>
    </div>
  );
}

export function AgentRunPage() {
  const { runId } = useParams<{ runId: string }>();
  const location = useLocation();
  const goal = (location.state as NavigationState | null)?.goal ?? null;

  const [run, setRun] = useState<AgentRun | null>(null);
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [artifacts, setArtifacts] = useState<AgentArtifact[]>([]);
  const [reviews, setReviews] = useState<AgentReview[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [controlPending, setControlPending] = useState(false);
  // Bumping this re-runs the polling effect (used after control actions).
  const [pollGeneration, setPollGeneration] = useState(0);
  const reviewsLoadedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!runId) return;
    let stopped = false;
    let timer = 0;

    async function tick() {
      try {
        const [runData, taskData, artifactData] = await Promise.all([
          getAgentRun(runId!),
          listAgentRunTasks(runId!),
          listAgentRunArtifacts(runId!),
        ]);
        if (stopped) return;
        setRun(runData);
        setTasks(taskData);
        setArtifacts(artifactData);
        setError(null);
        // Reviews exist for failed runs too (the verdict explains the failure);
        // fetch them once per run for either status.
        if (REVIEW_FETCH_STATUSES.has(runData.status) && reviewsLoadedFor.current !== runData.id) {
          reviewsLoadedFor.current = runData.id;
          const reviewData = await listAgentRunReviews(runId!);
          if (!stopped) setReviews(reviewData);
        }
        if (!TERMINAL_RUN_STATUSES.has(runData.status)) {
          timer = window.setTimeout(() => void tick(), 2000);
        }
      } catch (err) {
        if (stopped) return;
        setError(err instanceof ApiError ? err.message : "加载失败，请稍后重试");
        // Keep polling through transient errors.
        timer = window.setTimeout(() => void tick(), 2000);
      }
    }

    void tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [runId, pollGeneration]);

  async function handleControl(action: RunControlAction) {
    if (!runId || controlPending) return;
    setControlPending(true);
    setError(null);
    try {
      const updated = await controlAgentRun(runId, action);
      setRun(updated);
      // Restart polling if the run moved back into an active status.
      setPollGeneration((n) => n + 1);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "操作失败，请稍后重试");
    } finally {
      setControlPending(false);
    }
  }

  const controls = run ? controlsFor(run.status) : [];

  return (
    <div className="mx-auto w-full max-w-[760px] px-8 py-10">
      {/* Header: goal summary + status + controls */}
      <div className="mb-8">
        <p className="mb-1 text-xs text-ink-secondary">集群运行 {runId ? `#${runId.slice(0, 8)}` : ""}</p>
        <h1 className="mb-3 line-clamp-3 text-xl font-bold leading-snug text-ink">
          {goal ?? (run ? `运行 ${run.id.slice(0, 8)}` : "加载中…")}
        </h1>
        <div className="flex flex-wrap items-center gap-2">
          {run && <RunStatusBadge status={run.status} />}
          {run?.terminal_reason && <span className="text-xs text-ink-secondary">{run.terminal_reason}</span>}
          <div className="ml-auto flex items-center gap-2">
            {controls.map((control) => (
              <button
                key={control.action}
                type="button"
                disabled={controlPending}
                onClick={() => handleControl(control.action)}
                className={controlButtonClass}
              >
                {control.label}
              </button>
            ))}
          </div>
        </div>
        {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
      </div>

      {/* Task cards */}
      <section className="mb-10">
        <h2 className="mb-3 text-sm text-ink-secondary">子任务</h2>
        {tasks.length === 0 ? (
          <p className="text-sm text-ink-secondary">{run ? "暂无子任务" : "加载中…"}</p>
        ) : (
          <div className="flex flex-col gap-3">
            {tasks.map((task) => (
              <TaskCard key={task.id} task={task} />
            ))}
          </div>
        )}
      </section>

      {/* Artifacts */}
      {artifacts.length > 0 && (
        <section className="mb-10">
          <h2 className="mb-3 text-sm text-ink-secondary">产出</h2>
          <div className="flex flex-col gap-3">
            {artifacts.map((artifact) => (
              <div key={artifact.id} className="rounded-2xl border border-line bg-white px-5 py-4">
                <p className="mb-1 text-xs text-ink-secondary">{artifact.artifact_type}</p>
                <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">{artifact.preview}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Reviews */}
      {reviews.length > 0 && (
        <section>
          <h2 className="mb-3 text-sm text-ink-secondary">评审结论</h2>
          <ul className="divide-y divide-line rounded-2xl border border-line bg-white">
            {reviews.map((review) => (
              <li key={review.id} className="flex items-center gap-3 px-5 py-3.5">
                <span className="text-sm text-ink">{roleLabel(review.reviewer_role)}</span>
                {review.conflict_group && (
                  <span className="text-xs text-ink-secondary">冲突组 {review.conflict_group}</span>
                )}
                <span className="ml-auto flex items-center gap-3">
                  <span className="text-xs text-ink-secondary">证据 {review.evidence.length} 条</span>
                  <span
                    className={`rounded-full px-2.5 py-1 text-xs ${
                      review.status === "PASS"
                        ? "bg-emerald-50 text-emerald-700"
                        : review.status === "CONFLICT" || review.status === "WARNING"
                          ? "bg-red-50 text-red-600"
                          : "bg-hover text-ink-secondary"
                    }`}
                  >
                    {REVIEW_STATUS_LABELS[review.status] ?? review.status}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
