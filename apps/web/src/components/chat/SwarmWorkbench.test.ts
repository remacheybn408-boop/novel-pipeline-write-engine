import { describe, expect, it } from "vitest";
import type { AgentTask } from "../../lib/api/agentRuns";
import { POLL_FAILURE_TOLERANCE, isOrchestratorOnlyRun, pollFailureNotice, stageOf } from "./SwarmWorkbench";

function task(taskKey: string): AgentTask {
  return {
    id: taskKey,
    task_key: taskKey,
    role: "role",
    status: "PENDING",
    attempts: 1,
    token_budget: null,
    depends_on: [],
  };
}

describe("stageOf", () => {
  it("maps analyze to the analyst lane, not the write lane", () => {
    expect(stageOf("analyze")).toBe("analyst");
  });

  it("maps select to the write lane", () => {
    expect(stageOf("select")).toBe("write");
  });

  it("maps review/merge/rewrite/recheck tasks to their lanes", () => {
    expect(stageOf("review_style")).toBe("review");
    expect(stageOf("merge")).toBe("revise");
    expect(stageOf("rewrite")).toBe("revise");
    expect(stageOf("recheck")).toBe("revise");
  });

  it("maps promise_* tasks to the promise lane", () => {
    expect(stageOf("promise_contract")).toBe("promise");
    expect(stageOf("promise_verify")).toBe("promise");
    expect(stageOf("promise_register")).toBe("promise");
  });
});

describe("isOrchestratorOnlyRun", () => {
  it("is true for an analyze run (single analyst-seat task)", () => {
    expect(isOrchestratorOnlyRun([task("analyze")])).toBe(true);
  });

  it("is false for a write pipeline run", () => {
    expect(isOrchestratorOnlyRun([task("planner"), task("select"), task("merge")])).toBe(false);
  });

  it("is false before tasks load (lanes keep showing while loading)", () => {
    expect(isOrchestratorOnlyRun([])).toBe(false);
  });
});

// 工作台轮询容错：单次网络抖动保持静默并继续轮询，连续多次失败才降级为一行警告。
describe("pollFailureNotice", () => {
  it("stays silent through transient blips", () => {
    expect(pollFailureNotice(0)).toBeNull();
    expect(pollFailureNotice(1)).toBeNull();
    expect(pollFailureNotice(POLL_FAILURE_TOLERANCE - 1)).toBeNull();
  });

  it("degrades to a one-line warning after repeated consecutive failures", () => {
    expect(pollFailureNotice(POLL_FAILURE_TOLERANCE)).toBeTruthy();
    expect(pollFailureNotice(POLL_FAILURE_TOLERANCE + 5)).toBe(pollFailureNotice(POLL_FAILURE_TOLERANCE));
  });
});
