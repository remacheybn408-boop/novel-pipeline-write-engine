/**
 * Shared agent-run display labels, used by the agent run detail page and the
 * swarm workbench in the chat sidebar.
 */

/** Chinese display names for known agent roles; unknown roles render as-is. */
export const ROLE_LABELS: Record<string, string> = {
  // 五司 seats (cluster role keys), labelled with their 雅名.
  orchestrator: "马歇尔 · 总调度",
  analyst: "福尔摩斯 · 分析",
  write: "莎士比亚 · 写作",
  review: "约翰逊 · 审校",
  revise: "米开朗基罗 · 改写",
  chief_planner: "主策划",
  story_architect: "故事架构",
  world_builder: "世界观",
  character_designer: "角色设计",
  timeline_analyst: "时间线",
  scene_writer: "场景写作",
  continuity_reviewer: "连续性评审",
  adversarial_reviewer: "对抗评审",
  style_editor: "文风编辑",
  merge_editor: "合并编辑",
  chief_editor: "主编",
  // New single-run write-pipeline task keys (used as role labels when the
  // task carries no friendlier role string).
  planner: "主策划",
  character: "角色设计",
  scene: "场景写作",
  // Fourth parallel draft seat: the 去 AI 味 specialist (persona
  // packs/personas/scene_d.md); labelled distinctly in the workbench.
  scene_d: "人味写作",
  review_continuity: "连续性评审",
  review_adversarial: "对抗评审",
  review_style: "文风评审",
  // 评审合议（约翰逊协作化）：merge_editor 角色主持三评审合议——去重
  // findings、裁定冲突组、产出按严重度排序的改写指令清单。
  review_council: "评审合议",
  merge: "合并编辑",
  rewrite: "改写",
  recheck: "复审",
  analyze: "大纲分析",
  // 分析三席位（福尔摩斯协作化）：结构/人物/伏笔三个专项 analyst 并行，
  // analyze_merge（merge_editor 角色）融合成最终逐章工作流。
  analyze_structure: "结构分析",
  analyze_cast: "人物分析",
  analyze_hooks: "伏笔分析",
  analyze_merge: "大纲融合",
  // The draft-fusion task reuses the merge_editor role but is not a merge:
  // it synthesizes one final chapter from the parallel drafts (collaborative
  // fusion, deterministic pick only as a fallback).
  select: "融合择优",
  // 奥莉维亚 · 动态承诺 (promise keeper) role and its three pipeline tasks.
  promise_keeper: "奥莉维亚 · 动态承诺",
  promise_contract: "承诺契约",
  promise_verify: "承诺核对",
  promise_register: "承诺登记",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

/** Task display label: task_key wins when it carries its own label (e.g.
 *  "select" reuses merge_editor's role), otherwise the role label. */
export function taskLabel(task: { task_key: string; role: string }): string {
  return ROLE_LABELS[task.task_key] ?? roleLabel(task.role);
}

export const RUN_STATUS_LABELS: Record<string, string> = {
  PENDING: "待启动",
  RUNNING: "进行中",
  PAUSED: "已暂停",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
  BUDGET_EXHAUSTED: "预算耗尽",
};
