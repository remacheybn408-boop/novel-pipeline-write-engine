import { describe, expect, it } from "vitest";
import type { WritingChapter, WritingStatus } from "../../lib/api/writing";
import { canDownloadChapter, chapterBadge, downloadableChapters, promiseNodeBadge } from "./WritingProgressPanel";

function chapter(overrides: Partial<WritingChapter>): WritingChapter {
  return {
    chapter_no: 1,
    title: "第一章",
    chapter_id: "c1",
    status: "not_started",
    stage: "未开始",
    downloadable: false,
    ...overrides,
  };
}

describe("chapterBadge", () => {
  it("maps every status to its 徽标 (label + tone)", () => {
    expect(chapterBadge("not_started").label).toBe("未开始");
    expect(chapterBadge("writing").label).toBe("写作中");
    expect(chapterBadge("reviewing").label).toBe("审校中");
    expect(chapterBadge("rewriting").label).toBe("改写中");
    expect(chapterBadge("completed").label).toBe("完成");
    expect(chapterBadge("failed").label).toBe("失败");
  });

  it("uses the spec'd tone per status (灰/蓝/黄/紫/绿/红)", () => {
    expect(chapterBadge("not_started").className).toContain("bg-hover/60");
    expect(chapterBadge("writing").className).toContain("blue");
    expect(chapterBadge("reviewing").className).toContain("amber");
    expect(chapterBadge("rewriting").className).toContain("violet");
    expect(chapterBadge("completed").className).toContain("emerald");
    expect(chapterBadge("failed").className).toContain("red");
  });
});

describe("canDownloadChapter", () => {
  it("is enabled only for a completed chapter with a backing chapter row", () => {
    expect(canDownloadChapter(chapter({ status: "completed", downloadable: true }))).toBe(true);
  });

  it("is disabled for in-progress, failed, and plan-only chapters", () => {
    expect(canDownloadChapter(chapter({ status: "writing" }))).toBe(false);
    expect(canDownloadChapter(chapter({ status: "failed" }))).toBe(false);
    expect(canDownloadChapter(chapter({ status: "not_started" }))).toBe(false);
    // Plan-only chapter (no chapters-table row yet): nothing to export.
    expect(canDownloadChapter(chapter({ status: "completed", downloadable: true, chapter_id: null }))).toBe(false);
    // Backend-flag-off chapters stay disabled even if the status claims completed.
    expect(canDownloadChapter(chapter({ status: "completed", downloadable: false }))).toBe(false);
  });
});

describe("downloadableChapters", () => {
  it("counts only completed chapters for the 下载全部已完成 button", () => {
    const status: WritingStatus = {
      project_id: "p1",
      total_chapters: 3,
      current_chapter_no: 2,
      chapters: [
        chapter({ chapter_no: 1, status: "completed", downloadable: true }),
        chapter({ chapter_no: 2, status: "writing", chapter_id: null }),
        chapter({ chapter_no: 3, status: "not_started", chapter_id: null }),
      ],
    };
    expect(downloadableChapters(status).map((entry) => entry.chapter_no)).toEqual([1]);
  });
});

describe("promiseNodeBadge", () => {
  it("maps 奥莉维亚 node statuses to 徽标 (✓/…中/待/✗/—)", () => {
    expect(promiseNodeBadge("SUCCEEDED")).toMatchObject({ text: "✓" });
    expect(promiseNodeBadge("RUNNING")).toMatchObject({ text: "…中" });
    expect(promiseNodeBadge("PENDING")).toMatchObject({ text: "待" });
    expect(promiseNodeBadge("FAILED")).toMatchObject({ text: "✗" });
    expect(promiseNodeBadge(null)).toMatchObject({ text: "—" });
  });

  it("uses the spec'd tone per status (绿/蓝/灰/红)", () => {
    expect(promiseNodeBadge("SUCCEEDED").className).toContain("emerald");
    expect(promiseNodeBadge("RUNNING").className).toContain("blue");
    expect(promiseNodeBadge("FAILED").className).toContain("red");
  });
});
