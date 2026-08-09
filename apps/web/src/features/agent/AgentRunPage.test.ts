import { describe, expect, it } from "vitest";
import { REVIEW_FETCH_STATUSES, TASK_STATUS_LABELS } from "./AgentRunPage";
import type { AgentTaskStatus } from "../../lib/api/agentRuns";

describe("TASK_STATUS_LABELS", () => {
  it("covers every backend task status, including SKIPPED", () => {
    const statuses: AgentTaskStatus[] = ["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED"];
    for (const status of statuses) {
      expect(TASK_STATUS_LABELS[status]).toBeTruthy();
    }
  });
});

describe("REVIEW_FETCH_STATUSES", () => {
  it("fetches review verdicts for FAILED runs too, not only COMPLETED", () => {
    expect(REVIEW_FETCH_STATUSES.has("COMPLETED")).toBe(true);
    expect(REVIEW_FETCH_STATUSES.has("FAILED")).toBe(true);
  });

  it("does not fetch reviews for active or cancelled runs", () => {
    expect(REVIEW_FETCH_STATUSES.has("RUNNING")).toBe(false);
    expect(REVIEW_FETCH_STATUSES.has("CANCELLED")).toBe(false);
  });
});
