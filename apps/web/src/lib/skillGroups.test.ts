import { describe, expect, it } from "vitest";
import { groupSkills } from "./skillGroups";
import type { Skill } from "./api/plugins";

function skill(overrides: Partial<Skill>): Skill {
  return {
    id: overrides.id ?? "id",
    skill_key: null,
    name: "name",
    description: "",
    content: "",
    enabled: false,
    builtin: false,
    category: null,
    created_at: null,
    ...overrides,
  };
}

describe("groupSkills", () => {
  it("splits built-ins into 小说类 / 工具类 and user skills into 我的 Skills", () => {
    const skills = [
      skill({ id: "1", builtin: true, skill_key: "wuxia-fiction-writing", category: "fiction" }),
      skill({ id: "2", builtin: true, skill_key: "craft-foreshadowing", category: "tool" }),
      skill({ id: "3", builtin: true, skill_key: "builtin-narrative-rag", category: "tool" }),
      skill({ id: "4", name: "我的自定义" }),
    ];

    const groups = groupSkills(skills);

    expect(groups.map((group) => group.key)).toEqual(["fiction", "tool", "mine"]);
    expect(groups[0].skills.map((item) => item.id)).toEqual(["1"]);
    expect(groups[1].skills.map((item) => item.id)).toEqual(["2", "3"]);
    expect(groups[2].skills.map((item) => item.id)).toEqual(["4"]);
  });

  it("drops empty groups", () => {
    const groups = groupSkills([skill({ id: "1", name: "只有自建" })]);
    expect(groups.map((group) => group.key)).toEqual(["mine"]);
  });

  it("falls back to skill_key when category is missing (older backend)", () => {
    const groups = groupSkills([
      skill({ id: "1", builtin: true, skill_key: "xianxia-fiction-writing", category: null }),
      skill({ id: "2", builtin: true, skill_key: "craft-pacing-control", category: null }),
    ]);
    expect(groups.map((group) => group.key)).toEqual(["fiction", "tool"]);
  });
});
