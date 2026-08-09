import type { Skill } from "./api/plugins";

export interface SkillGroup {
  key: "fiction" | "tool" | "mine";
  label: string;
  skills: Skill[];
}

/** Skills tab grouping: built-ins split into 小说类 (genre packs) and 工具类
 *  (craft/system packs) by the server-supplied category (skill_key fallback
 *  for older backends); user-created skills land in 我的 Skills. Empty
 *  groups are dropped so the section never renders a bare header. */
export function groupSkills(skills: Skill[]): SkillGroup[] {
  const groups: SkillGroup[] = [
    { key: "fiction", label: "小说类", skills: [] },
    { key: "tool", label: "工具类", skills: [] },
    { key: "mine", label: "我的 Skills", skills: [] },
  ];
  for (const skill of skills) {
    let key: SkillGroup["key"] = "mine";
    if (skill.builtin) {
      const category = skill.category ?? (skill.skill_key?.endsWith("-fiction-writing") ? "fiction" : "tool");
      key = category === "fiction" ? "fiction" : "tool";
    }
    groups.find((group) => group.key === key)?.skills.push(skill);
  }
  return groups.filter((group) => group.skills.length > 0);
}
