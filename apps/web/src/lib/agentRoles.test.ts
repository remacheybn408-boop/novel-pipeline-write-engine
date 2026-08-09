import { describe, expect, it } from "vitest";
import { roleLabel, taskLabel } from "./agentRoles";

describe("taskLabel", () => {
  it("labels the select task as 融合择优 instead of its merge_editor role", () => {
    expect(taskLabel({ task_key: "select", role: "merge_editor" })).toBe("融合择优");
  });

  it("labels the fourth draft seat as 人味写作", () => {
    expect(taskLabel({ task_key: "scene_d", role: "scene_writer" })).toBe("人味写作");
  });

  it("falls back to the role label when the task_key has no own label", () => {
    expect(taskLabel({ task_key: "scene_a", role: "scene_writer" })).toBe("场景写作");
  });

  it("prefers a task_key label when both exist", () => {
    expect(taskLabel({ task_key: "merge", role: "merge_editor" })).toBe("合并编辑");
  });

  it("labels the promise keeper role and its three pipeline tasks", () => {
    expect(roleLabel("promise_keeper")).toBe("奥莉维亚 · 动态承诺");
    expect(taskLabel({ task_key: "promise_contract", role: "promise_keeper" })).toBe("承诺契约");
    expect(taskLabel({ task_key: "promise_verify", role: "promise_keeper" })).toBe("承诺核对");
    expect(taskLabel({ task_key: "promise_register", role: "promise_keeper" })).toBe("承诺登记");
  });

  it("labels the review council and the three analyze seats", () => {
    // 评审合议（约翰逊协作化）与分析三席位（福尔摩斯协作化）的任务级标签。
    expect(taskLabel({ task_key: "review_council", role: "merge_editor" })).toBe("评审合议");
    expect(taskLabel({ task_key: "analyze_structure", role: "analyst" })).toBe("结构分析");
    expect(taskLabel({ task_key: "analyze_cast", role: "analyst" })).toBe("人物分析");
    expect(taskLabel({ task_key: "analyze_hooks", role: "analyst" })).toBe("伏笔分析");
    expect(taskLabel({ task_key: "analyze_merge", role: "merge_editor" })).toBe("大纲融合");
  });

  it("renders unknown roles as-is", () => {
    expect(taskLabel({ task_key: "mystery", role: "mystery_role" })).toBe("mystery_role");
  });
});

describe("roleLabel", () => {
  it("still maps merge_editor to 合并编辑", () => {
    expect(roleLabel("merge_editor")).toBe("合并编辑");
  });

  it("labels the five cluster seats with their 雅名", () => {
    expect(roleLabel("orchestrator")).toBe("马歇尔 · 总调度");
    expect(roleLabel("analyst")).toBe("福尔摩斯 · 分析");
    expect(roleLabel("write")).toBe("莎士比亚 · 写作");
    expect(roleLabel("review")).toBe("约翰逊 · 审校");
    expect(roleLabel("revise")).toBe("米开朗基罗 · 改写");
  });
});
