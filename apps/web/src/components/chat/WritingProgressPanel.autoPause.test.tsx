// @vitest-environment jsdom
/**
 * Auto-pause banner UX: writing-status returns auto_pause (executor paused
 * the run after repeated provider failures) -> the panel shows a 「模型访问
 * 不稳定已暂停」 banner with a 恢复 button that calls the resume endpoint
 * and refetches. No auto_pause -> no banner. API modules are mocked.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WritingStatus } from "../../lib/api/writing";

const getWritingStatus = vi.fn<() => Promise<WritingStatus>>();
const controlAgentRun = vi.fn<(runId: string, action: string) => Promise<unknown>>();

vi.mock("../../lib/api/writing", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../lib/api/writing")>();
  return { ...original, getWritingStatus: (...args: unknown[]) => getWritingStatus(...(args as [])) };
});
vi.mock("../../lib/api/agentRuns", () => ({
  controlAgentRun: (...args: unknown[]) => controlAgentRun(...(args as [string, string])),
}));

const { WritingProgressPanel } = await import("./WritingProgressPanel");

function statusWithAutoPause(autoPause: WritingStatus["auto_pause"]): WritingStatus {
  return {
    project_id: "p1",
    total_chapters: 1,
    current_chapter_no: 1,
    chapters: [
      { chapter_no: 1, title: "第一章", chapter_id: "c1", status: "writing", stage: "已暂停", downloadable: false },
    ],
    auto_pause: autoPause,
  };
}

afterEach(() => {
  cleanup();
  getWritingStatus.mockReset();
  controlAgentRun.mockReset();
});

describe("WritingProgressPanel auto-pause banner", () => {
  it("auto_pause 非空 → 渲染提示条 + 恢复按钮，点击调 resume 并重新拉取", async () => {
    const autoPause = { run_id: "run-1", reason: "HTTP 503", provider: "openai", model: "gpt-4.1-mini", streak: 3 };
    getWritingStatus.mockResolvedValue(statusWithAutoPause(autoPause));
    controlAgentRun.mockResolvedValue({});

    render(<WritingProgressPanel projectId="p1" />);

    const resumeButton = await screen.findByRole("button", { name: "恢复" });
    expect(screen.getByText(/模型访问不稳定已暂停/)).toBeTruthy();
    await userEvent.click(resumeButton);

    await waitFor(() => expect(controlAgentRun).toHaveBeenCalledWith("run-1", "resume"));
    // A successful resume triggers an immediate refetch (polling continues).
    await waitFor(() => expect(getWritingStatus.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("auto_pause 为 null（人工暂停或健康）→ 不渲染恢复条", async () => {
    getWritingStatus.mockResolvedValue(statusWithAutoPause(null));

    render(<WritingProgressPanel projectId="p1" />);

    await screen.findByText(/第1章/);
    expect(screen.queryByRole("button", { name: "恢复" })).toBeNull();
    expect(screen.queryByText(/模型访问不稳定已暂停/)).toBeNull();
    expect(controlAgentRun).not.toHaveBeenCalled();
  });
});
