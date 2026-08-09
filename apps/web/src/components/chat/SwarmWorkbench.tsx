import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  TERMINAL_RUN_STATUSES,
  getAgentRun,
  listAgentRunTasks,
  type AgentRun,
  type AgentTask,
} from "../../lib/api/agentRuns";
import { RUN_STATUS_LABELS, taskLabel } from "../../lib/agentRoles";

/** Pipeline stages shown in the workbench, in display order. */
const STAGE_LANES = [
  { key: "analyst", label: "分析" },
  { key: "write", label: "写作" },
  { key: "review", label: "审校" },
  { key: "revise", label: "改写" },
  { key: "promise", label: "承诺" },
] as const;

type StageKey = (typeof STAGE_LANES)[number]["key"] | "orchestrator";

/** Group tasks into stages by task_key semantics (unknown keys fall back to 写作).
 *  "analyze" maps to the analyst lane: the 分析 seat groups every
 *  analyst-role task (outline拆解) of its own. */
export function stageOf(taskKey: string): StageKey {
  if (taskKey === "analyze") return "analyst";
  if (taskKey.startsWith("review")) return "review";
  if (taskKey === "merge" || taskKey === "rewrite" || taskKey === "recheck") return "revise";
  if (taskKey.startsWith("promise_")) return "promise";
  return "write";
}

/** True when every task belongs to the dispatcher seats (orchestrator/analyst,
 *  e.g. analyze runs): the write/review/revise lanes have nothing to show and
 *  would sit on 待办 forever, so the workbench hides the lanes and lists the
 *  tasks under 总调度. */
export function isOrchestratorOnlyRun(tasks: AgentTask[]): boolean {
  return tasks.length > 0 && tasks.every((task) => stageOf(task.task_key) === "orchestrator" || stageOf(task.task_key) === "analyst");
}

const cardClass = "rounded-2xl border border-line bg-white/60 p-4 backdrop-blur-sm";

/** Transient poll failures tolerated before the workbench degrades to a
 *  one-line warning (a single network blip must not hide the run). */
export const POLL_FAILURE_TOLERANCE = 3;

/** One-line warning shown after repeated poll failures; null while the
 *  failures are still within the transient tolerance. */
export function pollFailureNotice(consecutiveFailures: number): string | null {
  return consecutiveFailures >= POLL_FAILURE_TOLERANCE ? "连接中断，正在自动重试…" : null;
}

/** Lane badge from its member tasks: failed > running > done > todo.
 *  SKIPPED counts as done (the gate let the pipeline skip those tasks). */
function laneTone(tasks: AgentTask[]): { className: string; label: string } {
  if (tasks.some((task) => task.status === "FAILED")) {
    return { className: "bg-red-100 text-red-600", label: "失败" };
  }
  if (tasks.some((task) => task.status === "RUNNING")) {
    return { className: "bg-hover text-ink animate-pulse", label: "进行中" };
  }
  if (tasks.length > 0 && tasks.every((task) => task.status === "SUCCEEDED" || task.status === "SKIPPED")) {
    return { className: "bg-emerald-100 text-emerald-700", label: "完成" };
  }
  return { className: "bg-hover/60 text-ink-secondary", label: "待办" };
}

function TaskStatusDot({ status }: { status: string }) {
  const tone =
    status === "FAILED"
      ? "bg-red-500"
      : status === "RUNNING"
        ? "bg-ink animate-pulse"
        : status === "SUCCEEDED"
          ? "bg-emerald-500"
          : status === "SKIPPED"
            ? "bg-disabled opacity-50"
            : "bg-disabled";
  return <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone}`} />;
}

/**
 * Semi-transparent live workbench docked on the chat page's right side while
 * a swarm run is active (or finished). Polls the run + its tasks every 2s
 * until a terminal status, then stops; bumping `pollGeneration` (e.g. after a
 * retry flipped the run back to RUNNING) re-arms the loop, since the
 * component does not remount with an unchanged key. Transient poll failures
 * keep the UI and keep polling; only repeated failures degrade to a one-line
 * warning. Chatting on the left never disturbs it.
 */
export function SwarmWorkbench({
  runId,
  goal,
  pollGeneration = 0,
}: {
  runId: string;
  goal?: string;
  pollGeneration?: number;
}) {
  const [run, setRun] = useState<AgentRun | null>(null);
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stopped = false;
    let timer = 0;
    let consecutiveFailures = 0;
    async function tick() {
      try {
        const [runData, taskData] = await Promise.all([getAgentRun(runId), listAgentRunTasks(runId)]);
        if (stopped) return;
        consecutiveFailures = 0;
        setRun(runData);
        setTasks(taskData);
        setError(null);
        if (!TERMINAL_RUN_STATUSES.has(runData.status)) {
          timer = window.setTimeout(() => void tick(), 2000);
        }
      } catch {
        // Keep the UI alive through transient errors and keep polling; only
        // repeated consecutive failures surface a one-line warning.
        if (stopped) return;
        consecutiveFailures += 1;
        setError(pollFailureNotice(consecutiveFailures));
        timer = window.setTimeout(() => void tick(), 2000);
      }
    }
    void tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [runId, pollGeneration]);

  const orchestratorOnly = isOrchestratorOnlyRun(tasks);
  const runTone = !run
    ? "bg-hover/60 text-ink-secondary"
    : run.status === "FAILED"
      ? "bg-red-100 text-red-600"
      : run.status === "COMPLETED"
        ? "bg-emerald-100 text-emerald-700"
        : "bg-hover text-ink animate-pulse";

  return (
    <aside className="flex h-full w-[300px] shrink-0 flex-col gap-3 overflow-y-auto px-4 py-6">
      {/* Chief planner / run overview */}
      <section className={cardClass}>
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-ink">总调度</h2>
          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${runTone}`}>
            {run ? (RUN_STATUS_LABELS[run.status] ?? run.status) : "加载中…"}
          </span>
        </div>
        {goal && (
          <p className="mt-2 line-clamp-2 text-xs leading-5 text-ink-secondary" title={goal}>
            {goal}
          </p>
        )}
        {run?.gate === true && <p className="mt-2 text-xs text-ink-secondary/70">门禁：通过（无需改写）</p>}
        {run?.gate === false && <p className="mt-2 text-xs text-ink-secondary/70">门禁：未过（已自动改写）</p>}
        {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
        {orchestratorOnly && (
          <ul className="mt-2 flex flex-col gap-1.5">
            {tasks.map((task) => (
              <li
                key={task.id}
                className="flex items-center gap-2 text-xs text-ink-secondary"
                title={task.last_error ?? taskLabel(task)}
              >
                <TaskStatusDot status={task.status} />
                <span className="min-w-0 flex-1 truncate">
                  {taskLabel(task)}
                  {task.status === "SKIPPED" && <span className="ml-1 opacity-60">跳过</span>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Pipeline stages: analyst / write / review / revise (hidden for
          dispatcher-only runs, whose tasks list under 总调度 above) */}
      {!orchestratorOnly &&
        STAGE_LANES.map((lane) => {
        const laneTasks = tasks.filter((task) => stageOf(task.task_key) === lane.key);
        const tone = laneTone(laneTasks);
        return (
          <section key={lane.key} className={cardClass}>
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-sm font-semibold text-ink">{lane.label}</h2>
              <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${tone.className}`}>
                {tone.label}
              </span>
            </div>
            {laneTasks.length > 0 && (
              <ul className="mt-2 flex flex-col gap-1.5">
                {laneTasks.map((task) => (
                  <li
                    key={task.id}
                    className="flex items-center gap-2 text-xs text-ink-secondary"
                    title={task.last_error ?? taskLabel(task)}
                  >
                    <TaskStatusDot status={task.status} />
                    <span className="min-w-0 flex-1 truncate">
                      {taskLabel(task)}
                      {task.status === "SKIPPED" && <span className="ml-1 opacity-60">跳过</span>}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        );
      })}

      <div className="mt-auto pt-2 text-center">
        <Link to={`/agent-runs/${runId}`} className="text-xs text-ink-secondary underline transition-colors hover:text-ink">
          查看详情
        </Link>
      </div>
    </aside>
  );
}
