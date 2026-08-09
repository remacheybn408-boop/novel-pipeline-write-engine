---
name: builtin-narrative-rag
description: Inject the narrative RAG scene pack (worldview, current state, constraints, long-range evidence) into Work-mode writing contexts. Disable to fall back to the legacy context injection.
---

# 小说检索场景包（Narrative RAG）

控制 Work 模式写作时的 RAG 场景包注入。

开启（默认）时，写作上下文会注入四段场景包：

- **[世界观与设定]**：pinned Story Bible 条目、其余设定（按置信度降序）、角色表（人工角色优先于自动提取）；
- **[当前状态]**：最新章、最近 5 章摘要、最新章出场角色、未结伏笔；
- **[写作约束]**：当前章号与本章目标；
- **[长篇事实证据]**：向量 + 关键词混合检索（RRF 融合）的章节证据块，标注来源章号。

关闭时回退到旧版上下文注入（故事事实触发 + 上下文条目），不注入场景包。

权威排序：用户 pinned > Story Bible > 已采纳章节 > 草稿 > AI 自动提取。
