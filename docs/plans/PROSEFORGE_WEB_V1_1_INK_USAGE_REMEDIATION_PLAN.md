# ProseForge Web v1.1 水墨界面、Token 用量与产品化整改执行计划

> 执行对象：Kimi Code / Codex 类自动编程 Agent  
> 仓库：`remacheybn408-boop/ProseForge`  
> 审计基线分支：`master`  
> 审计基线提交：`f6183d17f9a3d46eb42c6ed3c8a6ae2e135dc6a5`  
> 新开发分支：`feat/web-v1.1-ink-usage`  
> 执行方式：按任务顺序连续完成，不按工作日拆分，不因常规实现选择向用户反复确认。

---

## 0. 执行契约

1. 开始前读取根目录 `AGENTS.md`、本计划和现有 `docs/plans/PROSEFORGE_WEB_V1_CODEX_PLAN.md`。
2. 从最新 `master` 创建 `feat/web-v1.1-ink-usage`，禁止直接在 `master` 开发。
3. 本计划优先级高于旧计划中已经过期的分支名称和 Web 临时实现方式；架构红线继续有效。
4. 每项任务执行固定流程：先写失败测试、确认失败、最小实现、确认通过、提交。
5. 不得删除、跳过或弱化现有测试来换取绿色结果。
6. 不得伪造 Token、费用、工作流进度或连接状态。拿不到厂商真实用量时必须标记为“估算”。
7. 不得把 API Key、完整 Prompt、小说正文或用户隐私写入日志、审计记录和错误响应。
8. 不得继续向 `apps/web/src/main.tsx` 堆叠业务功能；先拆分架构，再实现新页面。
9. 不得用大面积纯黑背景冒充水墨风。界面以宣纸白、墨灰、留白为主，朱砂仅作极少量强调。
10. 每阶段完成后提交并继续下一阶段；只有遇到真实不可执行、安全风险或数据破坏风险才停止。
11. 最终必须给出真实执行结果、失败项和未完成项；不得只更新文档后宣称完成。

---

# 1. 最终目标与完成定义

## 1.1 产品目标

将当前“功能已经接通但仍像内部 Demo”的 Web v1，整改为普通用户可以长期写小说的自托管产品：

- Web 视觉统一为黑白水墨书卷风，清爽、克制、有留白，不使用廉价的大色块和通用后台模板感。
- 写作工作台能稳定编辑，不覆盖未保存内容，支持可靠草稿、版本、导出和恢复。
- 用户随时能看到本次请求、当前会话、当前项目、当前工作流的 Token 消耗。
- 明确区分“上下文预算”“厂商实际 Token”“本地估算 Token”“估算费用”。
- 自动写章、审校、改写和聊天助手都进入同一套用量采集链路。
- 工作流真正支持暂停、继续、取消、重试、恢复、预算限制和实时状态。
- 写作助手实际使用章节正文、故事记忆、大纲、最近对话，而不是只发送最后一句用户消息。
- Provider、模型、凭据和角色配置由后端数据驱动，不在前端写死。
- 前后端结构清晰，测试能覆盖真实用户路径，而不是只验证页面能打开。

## 1.2 完成定义

以下条件全部满足才可宣布 v1.1 完成：

- [ ] `main.tsx` 只保留应用启动，业务页面已拆分。
- [ ] 编辑器连续输入 5 分钟不会被服务端版本回写覆盖。
- [ ] 草稿自动保存有防抖，刷新和异常退出后可恢复。
- [ ] Markdown 导出按钮真实可用，TypeScript 能发现未导入符号。
- [ ] 所有模型调用都生成一条可追踪的用量记录。
- [ ] OpenAI/Anthropic/Google/Chat Completions/Cohere/Ollama 等适配器的用量事件有统一格式。
- [ ] 没有真实用量的 Provider 使用本地估算并显示“估算”，不能显示为真实值。
- [ ] Web 顶部或右侧常驻 Token 指示器可查看本次、会话、项目和工作流累计。
- [ ] Context 页面显示已用、可用、占比、模型上下文窗口和超限预警。
- [ ] 工作流费用限制基于实际/可解释估算，而不是永远累加 `0`。
- [ ] 停止生成后 Worker 不再继续写 Chunk，也不会最终改成 `COMPLETED`。
- [ ] 工作流继续和重试会重新入队，状态实时更新。
- [ ] 1440px、1024px、768px、390px 宽度下核心功能均可用；AI 助手只能折叠，不能直接消失。
- [ ] 中文模式没有关键英文残留，英文模式没有关键中文残留。
- [ ] Docker 全量测试、类型检查、E2E、迁移、恢复和视觉回归全部通过。

---

# 2. 当前仓库审计结论

## 2.1 P0：必须先修复

### P0-1 编辑器可能覆盖用户正在输入的正文

位置：`apps/web/src/main.tsx` 的 `Studio`。

当前版本加载 Effect 依赖 `content`，而加载完成后只要服务端版本存在内容就再次 `setContent`。用户每次输入都会让 Effect 重建，并可能把本地新文字覆盖回已保存版本。

整改：

- 版本加载只依赖 `chapter.id` 和明确的刷新动作。
- 使用 `lastLoadedVersionId`、`dirty`、`serverRevision` 区分服务端正文和本地草稿。
- 服务端更新与本地脏数据冲突时弹出比较/恢复选择，禁止静默覆盖。
- 草稿保存增加 500—800ms 防抖，切章和关闭页面时 flush。

### P0-2 导出存在运行时未定义风险

位置：`apps/web/src/main.tsx` 调用 `requestExport`，但顶部导入列表没有该符号。

根因：当前 `pnpm build` 只执行 Vite，未执行 `tsc --noEmit`，Vite 构建不能代替完整类型检查。

整改：

- 补齐导入。
- 新增 `pnpm typecheck`。
- `web-test` 和 GitHub Actions 强制执行 `typecheck -> test -> build`。
- 新增真实点击 Markdown 导出的组件测试和 Playwright E2E。

### P0-3 实际 Token 事件被丢弃

位置：

- `proseforge/providers/openai.py`
- `proseforge/providers/chat_completions.py`
- `proseforge/application/conversations/generate_reply.py`
- `proseforge/workflows/novel_generation.py`

Provider 已产生 `usage.updated` 或在完成事件中携带 usage，但 `GenerateReply`、写章、审校和改写逻辑只处理 `content.delta`，其余事件全部跳过。数据库没有足够字段记录输入、输出、缓存、推理 Token 和费用。

整改：建立“事件归一化 → 累计 → 持久化 → 聚合 API → SSE → Web 展示”的完整链路，详见第 5 节。

### P0-4 停止生成不能真正停止 Worker

位置：

- `proseforge/api/routes/conversations.py`
- `proseforge/application/conversations/generate_reply.py`

API 只把消息状态改为 `CANCELLED`；Worker 在每个 Chunk 之间不检查数据库状态，仍会继续生成、写入并最终设为 `COMPLETED`。

整改：

- 每 N 个 Chunk 或每 250ms 检查取消标记。
- 发现取消后关闭流、停止写 Chunk、保留已生成部分并维持 `CANCELLED`。
- `GenerateReply` 完成前使用条件更新，禁止覆盖已取消状态。
- 增加取消竞态集成测试。

### P0-5 不支持的 Provider 可能让消息永久停在 PENDING

位置：`proseforge/workflows/celery_app.py::_generate_chat`。

`build_provider` 抛出 `KeyError` 时直接返回，没有把消息更新为失败状态。

整改：

- 所有提前返回路径统一进入消息终态处理。
- 无输出时为 `FAILED`；已有部分输出时为 `PARTIAL`。
- 保存可读错误代码，不保存密钥和完整 Prompt。

### P0-6 工作流继续/重试没有重新入队

位置：`proseforge/api/routes/workflows.py`。

`resume` 和 `retry` 只改变数据库状态，没有重新提交 Celery 任务；`/events` 只发送已有事件后结束，并非实时订阅。

整改：

- 控制接口获取 `Request` 和 Queue，在合法状态变化后幂等重新入队。
- 增加 `task_id`、重试次数和最后错误字段。
- 工作流 SSE 使用持久事件流持续订阅，支持 `Last-Event-ID` 重连。
- Web 不再依赖静态状态或盲目轮询。

## 2.2 P1：产品必须整改

### P1-1 Web 读取了上下文 Token 但没有显示

`ContextView` 保存了 `used_tokens`，但 JSX 中完全未使用；`context_window` 和 `available_tokens` 也被丢弃。

### P1-2 上下文窗口固定写死为 128000

`proseforge/api/routes/context.py` 不读取当前模型配置，所有模型统一按 128K 计算，提示会误导用户。

### P1-3 工作流成本限制是空壳

数据库存在 `estimated_cost` 和 `cost_limit`，Repository 也能判断超限，但调用方每次 checkpoint 都没有传入真实成本，实际永远增加 `0`。工作流查询接口也不返回成本字段。

### P1-4 写作助手没有真正的小说上下文

聊天 Worker 只把最后一条用户消息放入 `GenerationRequest`。没有当前章节、故事记忆、大纲、角色设定和近期对话；自动写章调用 `run_writer_editor_loop` 时也没有传入 `context_text`。

### P1-5 Web 是单文件原型结构

绝大多数页面、请求、状态和行为都在 `apps/web/src/main.tsx` 中。继续添加 Token、工作流和水墨组件会迅速失控。

### P1-6 当前水墨风只是换色

现有样式主要是米白底、灰字、朱砂按钮和径向渐变，缺少宣纸层次、墨色浓淡、书卷布局、笔触分隔、内容焦点和一致组件语言。

### P1-7 响应式直接隐藏 AI 助手

900px 以下 `.review-pane { display: none; }`，核心功能被删除而不是折叠为抽屉或底部面板。

### P1-8 Provider 和模型配置仍是硬编码

设置页写死 Provider 数组，模型 ID 只能手填；后端已有 `/providers` 和 `/models`，前端却没有使用。

### P1-9 大纲补充逻辑写死创作参数

Web 把一次回答强制包装为：第三人称、12 章、每章 1500 字，无法按实际缺失问题逐项收集。

### P1-10 工作流默认只创建第 1 章

前端 `createWorkflow(project.id, [1])`，没有章节范围选择，也没有使用大纲计划的章节数。

### P1-11 中文本地化不完整

Email、Password、版本状态、工作流状态、错误消息、分支消息等仍有英文硬编码。API 客户端统一错误也只有英文。

### P1-12 登录和离线状态混淆

`listProjects` 任何失败都会被当成“未登录”，API 离线时用户会看到登录页而不是服务不可用提示；没有注销入口和统一 401 处理。

### P1-13 生产 Cookie 配置不安全

登录 Cookie 固定 `secure=False`，没有根据生产环境和 HTTPS 自动切换；缺少统一的 Origin/CSRF 防护策略和登录限速。

### P1-14 凭据管理不可维护

同一用户可为同一 Provider 新增多条凭据，页面没有更新、替换、删除入口；刷新后 masked key 只显示 `configured`，难以判断当前使用项。

### P1-15 前端测试无法证明产品可用

现有测试中包含只验证产品名称字符串的用例。核心编辑器、Token、取消、断线重连、移动端、动态模型和错误状态没有足够测试。

## 2.3 P2：稳定性与维护性整改

- `package.json` 使用多项 `latest`，应写入明确版本并保留 lockfile。
- API `request()` 默认对所有成功响应调用 `response.json()`，无法正确处理 204。
- Workflow、Project Inspector 多处显示静态文案，不代表真实状态。
- 模型目录是全局表，需明确单用户产品假设；若支持多用户，自定义模型和价格配置必须有 owner 边界。
- 生产 Compose 需要独立配置，数据库密码、主密钥和 JWT 不应依赖开发默认值。
- 需要结构化前端日志和错误边界，但禁止记录正文与 Prompt。

---

# 3. 目标架构

## 3.1 Web 目录结构

将 `apps/web/src/main.tsx` 拆为：

```text
apps/web/src/
  main.tsx
  app/
    App.tsx
    router.tsx
    queryClient.ts
    ErrorBoundary.tsx
    SessionProvider.tsx
  layouts/
    InkShell.tsx
    ProjectRail.tsx
    WorkspaceHeader.tsx
    AssistantDrawer.tsx
    InspectorPanel.tsx
  pages/
    LoginPage.tsx
    ProjectsPage.tsx
    StudioPage.tsx
    OutlinePage.tsx
    ContextPage.tsx
    WorkflowPage.tsx
    SettingsPage.tsx
    UsagePage.tsx
  features/
    auth/
    projects/
    editor/
    conversations/
    context/
    workflows/
    providers/
    usage/
    versions/
  components/
    ink/
      PaperPanel.tsx
      InkButton.tsx
      BrushDivider.tsx
      SealBadge.tsx
      EmptyScroll.tsx
      StatusStamp.tsx
    feedback/
      ToastRegion.tsx
      InlineError.tsx
      Skeleton.tsx
  lib/
    api/
      client.ts
      contracts.ts
      errors.ts
    i18n/
    drafts/
    formatting/
  styles/
    tokens.css
    reset.css
    shell.css
    components.css
    pages.css
```

规则：

- Server state 使用 TanStack Query；不得在每个页面手写重复的 loading/retry/cache。
- 路由使用 TanStack Router 或等价的类型安全 Router；刷新后能恢复当前页面和项目。
- 临时 UI 状态可使用 Zustand，小范围表单状态保留组件内。
- 正文编辑器使用 Tiptap 或稳定的独立 Editor 组件，数据库 canonical 内容仍保存 Markdown/纯文本，禁止把不可迁移 HTML 作为唯一正文格式。
- 样式使用自定义 CSS 变量和组件类，不套通用后台模板，不依赖外部收费字体或图片。
- `package.json` 所有依赖写明确版本，禁止 `latest`。

## 3.2 后端用量模块

```text
proseforge/
  domain/usage/
    entities.py
    normalization.py
    pricing.py
  application/usage/
    record_usage.py
    query_usage.py
    aggregate_usage.py
  infrastructure/database/models/usage.py
  infrastructure/database/repositories/usage.py
  api/routes/usage.py
  providers/usage.py
```

依赖方向：

```text
Provider raw event
  -> provider usage normalizer
  -> domain UsageDelta
  -> application UsageRecorder
  -> repository
  -> PostgreSQL
  -> usage API / durable SSE
  -> Web TokenMeter
```

Provider 不能直接写数据库；Application 不能 import FastAPI；API Route 不能解析厂商原始 usage。

---

# 4. 黑白水墨视觉规范

## 4.1 设计原则

1. “宣纸白”是主背景，不使用大面积黑底。
2. 墨色分为焦墨、浓墨、淡墨、飞白四级，用层次而不是彩色卡片区分区域。
3. 朱砂只用于主操作、危险提示、当前选中和印章式状态，占屏幕面积不得超过约 3%。
4. 页面保留明显留白，正文编辑区是视觉中心。
5. 水墨纹理必须克制，不影响文字对比度和滚动性能。
6. 不使用山水照片；纸纹、墨晕和笔触使用 CSS/SVG 自生成，避免版权和网络依赖。
7. 所有动效支持 `prefers-reduced-motion`。

## 4.2 设计 Token

```css
--paper-0: #fcfbf7;
--paper-1: #f5f2e9;
--paper-2: #e8e2d5;
--ink-100: #171715;
--ink-80: #343431;
--ink-60: #625f58;
--ink-40: #918c82;
--ink-20: rgba(23, 23, 21, 0.16);
--wash-1: rgba(23, 23, 21, 0.035);
--wash-2: rgba(23, 23, 21, 0.075);
--vermilion: #a64032;
--vermilion-dark: #7f2f26;
--success-ink: #465647;
--warning-ink: #765c35;
--shadow-paper: 0 18px 50px rgba(32, 29, 24, 0.09);
--radius-paper: 6px;
--radius-control: 4px;
```

字体栈：

```css
--font-ui: "Noto Sans SC", "Microsoft YaHei", system-ui, sans-serif;
--font-reading: "Source Han Serif SC", "Songti SC", "STSong", Georgia, serif;
```

不得把字体文件提交或分发到仓库；只使用系统字体栈。

## 4.3 页面布局

### 桌面端 ≥ 1200px

```text
左侧项目书架 216px
中央正文 minmax(620px, 1fr)
右侧助手/状态 340px，可折叠
```

- 左侧像竖向书签，不使用厚重卡片。
- 中央正文显示为轻微纸张层次，编辑器最大阅读宽度 760px。
- 右侧上方常驻 TokenMeter，下方为 AI 助手或 Inspector。
- 顶部显示项目名、章节名、保存状态、模型、上下文占比和本次 Token。

### 768px—1199px

- 左侧缩为图标/短标签栏。
- 右侧变为可拉出的抽屉，保留明显入口和未读/生成状态。
- 编辑器不缩成窄列。

### < 768px

- 顶部应用栏 + 底部主导航。
- 章节目录和 AI 助手为全屏 Sheet。
- Token 指示器显示简版数值，点击展开详情。
- 禁止直接 `display:none` 删除核心功能。

## 4.4 核心组件

- `InkShell`：应用骨架与纸纹背景。
- `PaperPanel`：轻边界、低阴影、无大圆角。
- `BrushDivider`：不规则但可控的墨线分隔。
- `InkButton`：默认墨线按钮、朱砂主按钮、危险操作描边按钮。
- `SealBadge`：已保存、已完成、已连接等印章式状态。
- `TokenMeter`：环形或横向墨条，支持实际/估算标记。
- `UsagePopover`：本次、会话、项目、工作流四层用量。
- `ContextBudgetBar`：上下文预算，不与实际消耗混淆。
- `AssistantDrawer`：桌面固定、平板抽屉、手机全屏。
- `EmptyScroll`：空状态使用简化卷轴构图，不使用通用插画。

## 4.5 各页面要求

### 登录页

- 居中宣纸登录单，不放大面积黑图。
- 首次初始化和普通登录明确区分。
- 显示 API/数据库状态，离线时不误导用户继续登录。

### 项目页

- 项目以“书册列表”呈现，显示章节进度、最近编辑时间、累计 Token。
- 新建项目是清晰主操作，不使用多层弹窗。

### 写作工作台

- 正文为唯一视觉焦点。
- 显示保存状态：`已保存 / 正在保存 / 未保存 / 冲突`。
- 章节列表可搜索、显示字数和状态。
- AI 助手消息支持停止、重试、继续、分支。
- TokenMeter 在生成时实时变化。

### Context 页面

- 顶部显示：当前模型、上下文窗口、已用、预留输出、可用。
- 条目显示单条估算 Token；支持置顶、排除、优先级、编辑、删除。
- 超过 70%/85%/95% 分别显示提醒、警告、阻止。

### Workflow 页面

- 显示每章状态、Writer/Editor/Rewriter 调用次数、Token、费用、重写轮次和最后错误。
- 暂停/继续/重试必须反映真实队列动作。
- 预算不足时状态为 `BUDGET_BLOCKED`，给出调整入口。

### Settings 页面

- Provider 来自 `/api/v1/providers`。
- 保存凭据后自动探测并同步模型。
- 模型通过可搜索下拉框选择，显示上下文窗口和能力。
- 凭据按 Provider 唯一，可替换、删除、重新测试。
- Writer/Editor 配置分别显示。

### Usage 页面

- 今日、近 7 天、当前项目、按 Provider、按模型、按用途统计。
- Token 数据始终展示；费用仅在有价格配置时展示。
- 实际与估算分开展示，不能相加后伪装为真实总数。

---

# 5. Token 与费用完整方案

## 5.1 统一用量结构

新增领域结构：

```python
@dataclass(frozen=True)
class UsageDelta:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    source: Literal["provider", "estimated"] = "provider"
    final: bool = False
    provider_request_id: str | None = None
    raw_metadata: dict[str, object] = field(default_factory=dict)
```

新增持久化表 `model_usage_records`：

- `id`
- `user_id`
- `project_id` nullable
- `conversation_id` nullable
- `message_id` nullable
- `workflow_run_id` nullable
- `workflow_step`
- `provider`
- `model_id`
- `provider_request_id` nullable
- `input_tokens`
- `output_tokens`
- `cached_input_tokens`
- `reasoning_tokens`
- `total_tokens`
- `cost_usd` nullable
- `usage_source`：`provider | estimated`
- `is_final`
- `latency_ms` nullable
- `created_at`
- `metadata`：只允许非敏感结构化字段

索引：

- `(user_id, created_at)`
- `(project_id, created_at)`
- `(conversation_id, created_at)`
- `(workflow_run_id, created_at)`
- `(message_id)`
- `(provider, model_id, created_at)`

幂等约束：优先使用 `(provider, provider_request_id, is_final)`；无 request ID 时使用内部 `call_id + sequence_no`。

## 5.2 Provider 归一化

为每个 Provider 编写小型 normalizer：

- OpenAI Responses：解析 `response.usage.updated` 和 `response.completed.response.usage`。
- OpenAI-compatible Chat Completions：解析 Chunk `usage`，请求时按兼容能力设置 `stream_options.include_usage`。
- Anthropic：解析 `message_start.usage`、`message_delta.usage`。
- Google：解析 `usageMetadata`。
- Cohere：解析 billed/input/output token 字段。
- Ollama/vLLM：解析 prompt/eval count；无字段时使用 provider.count_tokens 估算。
- 其他国内 Provider：按公开响应契约映射；无法提供真实 usage 时明确 `source=estimated`。

规则：

- `total_tokens` 缺失时由规范字段相加。
- 厂商多次发送累计值时做“最终覆盖”或差量去重，禁止重复累加。
- Provider 原始 usage 只允许保留非敏感字段。
- 使用官方数值优先于本地估算。

## 5.3 调用生命周期

每次模型调用：

1. 创建 `call_id`。
2. 使用 `provider.count_tokens(request)` 记录请求前估算，标记 `estimated`。
3. 记录开始时间、用途、项目/消息/工作流关联。
4. 流式处理内容和 usage 事件。
5. 收到最终真实 usage 后更新同一调用记录，替换估算字段。
6. 没收到真实 usage 时保留估算，并估算输出 Token。
7. 保存延迟和终态。
8. 发布脱敏 `usage.updated` SSE。

## 5.4 费用规则

- 不在代码中永久硬编码会过期的厂商价格。
- 模型目录允许配置：`input_price_per_million`、`output_price_per_million`、`cached_input_price_per_million`、币种和更新时间。
- 没有价格时 `cost_usd = null`，Web 显示“未配置价格”，不能显示 `$0.00`。
- 手动模型可在 Settings 中填写价格；修改价格只影响未来调用，历史记录保留当时计算结果。

## 5.5 工作流预算

- 保留 `cost_limit`，增加可选 `token_limit`。
- 每次 Writer/Editor/Rewriter 调用结束后，把实际或估算用量写入工作流累计。
- 下一次调用前检查预计增量：
  - 超过 `token_limit`：进入 `BUDGET_BLOCKED`。
  - 价格已知且超过 `cost_limit`：进入 `BUDGET_BLOCKED`。
  - 价格未知时不能假装费用可控；只执行 Token 限制并显示警告。
- 用户提高限制后可继续入队。

## 5.6 API 契约

新增：

```text
GET /api/v1/usage/summary
GET /api/v1/usage/records
GET /api/v1/projects/{project_id}/usage
GET /api/v1/conversations/{conversation_id}/usage
GET /api/v1/workflows/{workflow_id}/usage
```

`GET /api/v1/usage/summary` 示例：

```json
{
  "scope": "project",
  "project_id": "...",
  "actual": {
    "input_tokens": 12000,
    "output_tokens": 4500,
    "cached_input_tokens": 2000,
    "reasoning_tokens": 0,
    "total_tokens": 16500,
    "cost_usd": 0.42
  },
  "estimated": {
    "input_tokens": 800,
    "output_tokens": 200,
    "total_tokens": 1000,
    "cost_usd": null
  },
  "by_provider": [],
  "by_model": [],
  "updated_at": "..."
}
```

扩展：

- 消息列表返回每条助手消息的用量摘要。
- Workflow 响应返回 `estimated_cost`、`cost_limit`、`used_tokens`、`token_limit`、`checkpoint`、`last_error`。
- SSE 发布 `usage.updated`，只含数字、模型、用途和关联 ID。

## 5.7 Web 展示规则

常驻 TokenMeter：

```text
本次：输入 1.2K / 输出 640
会话：8.6K
项目：126K
上下文：34K / 128K
费用：$0.42（估算）
```

显示规则：

- `provider` 数据标记“实际”。
- `estimated` 数据标记“估算”。
- 上下文预算单独显示，不计入历史消耗。
- 数值使用 K/M 简写，悬停或点击显示完整数值。
- 流式过程中平滑更新，不每个 Chunk 触发整页重渲染。

---

# 6. 连续执行任务

## 阶段 0：建立安全基线和新门禁

### Task 0.1 创建分支并记录基线

- [ ] 拉取最新 `master`。
- [ ] 确认 HEAD 为本计划基线或更新计划中的实际 HEAD。
- [ ] 创建 `feat/web-v1.1-ink-usage`。
- [ ] 把旧 `AGENTS.md` 中固定 `feat/web-v1` 的规则改为当前分支或通用 `feat/*` 规则。
- [ ] 运行现有 Docker 全量基线并记录到 `artifacts/WEB_V1_1_BASELINE.md`。

提交：

```text
chore: establish web v1.1 remediation baseline
```

### Task 0.2 补齐前端质量门

修改：

- `apps/web/package.json`
- `compose.test.yaml`
- `.github/workflows/test.yml`
- `apps/web/tsconfig.json`

要求：

- [ ] 新增 `typecheck: tsc --noEmit`。
- [ ] 新增 `test:unit`、`test:e2e`、`build` 明确脚本。
- [ ] `web-test` 顺序为 `pnpm typecheck && pnpm test && pnpm build`。
- [ ] 明确依赖版本，删除所有 `latest`。
- [ ] 开启 `noUnusedLocals`、`noUnusedParameters`，逐步修复现有问题，不用关闭规则绕过。

提交：

```text
build: enforce frontend typecheck and pinned dependencies
```

## 阶段 1：修复 P0 运行时缺陷

### Task 1.1 修复编辑器覆盖和草稿写放大

新建/修改：

- `apps/web/src/features/editor/useChapterDocument.ts`
- `apps/web/src/features/editor/ChapterEditor.tsx`
- `apps/web/src/lib/drafts/`
- 对应单元测试和 E2E

验收：

- [ ] 输入过程中不触发服务端版本重新覆盖。
- [ ] 草稿写入有防抖。
- [ ] 切章前保存/flush。
- [ ] 有本地草稿与服务端新版本时显示冲突选择。
- [ ] 刷新恢复未保存正文。

提交：

```text
fix: protect chapter drafts from version reload overwrite
```

### Task 1.2 修复导出与 API 客户端

修改：

- `apps/web/src/lib/api/client.ts`
- Editor/Export 组件
- 导出测试

要求：

- [ ] 正确导入并调用 `requestExport`。
- [ ] `request()` 支持 204、非 JSON 成功响应和下载错误。
- [ ] 401 触发统一会话过期流程。
- [ ] Markdown 导出 E2E 验证下载内容，而不是只验证按钮存在。

提交：

```text
fix: make exports and api responses runtime safe
```

### Task 1.3 修复消息取消与失败终态

修改：

- `proseforge/application/conversations/generate_reply.py`
- `proseforge/workflows/celery_app.py`
- Conversation Repository 条件状态更新
- 对应集成测试

要求：

- [ ] Worker 识别取消并停止写入。
- [ ] 不支持 Provider、凭据缺失、请求失败均进入明确终态。
- [ ] `CANCELLED` 不会被 `COMPLETED` 覆盖。
- [ ] 有 Chunk 才允许 `PARTIAL`，无 Chunk 使用 `FAILED`。

提交：

```text
fix: make chat cancellation and terminal states durable
```

### Task 1.4 修复工作流控制闭环

修改：

- `proseforge/api/routes/workflows.py`
- `proseforge/infrastructure/database/repositories/workflow.py`
- `proseforge/workflows/celery_app.py`
- Workflow 测试

要求：

- [ ] resume/retry 幂等重新入队。
- [ ] cancel 后 Worker 在安全检查点停止。
- [ ] SSE 持续订阅并支持重连。
- [ ] 同一 Workflow 不产生并发重复任务。

提交：

```text
fix: reconnect workflow controls to durable execution
```

## 阶段 2：拆分 Web 架构

### Task 2.1 建立 App、Router、Query、Session 层

- [ ] 按第 3.1 节创建目录。
- [ ] `main.tsx` 只负责 render 和全局 Provider。
- [ ] 路由包含 login/projects/studio/outline/context/workflow/settings/usage。
- [ ] 当前项目和当前页面可通过 URL 恢复。
- [ ] API 离线、未登录、权限不足分开处理。
- [ ] 增加注销入口。

提交：

```text
refactor: split web application shell and server state
```

### Task 2.2 拆分功能页面和共享反馈组件

- [ ] 每个 Page 不超过合理复杂度，业务逻辑下沉到 feature hooks。
- [ ] 所有 mutation 有 loading、disabled、success、error 状态。
- [ ] 禁止空 catch。
- [ ] Toast 使用 `aria-live`，表单错误绑定字段。

提交：

```text
refactor: modularize proseforge web features
```

## 阶段 3：实现 Token 后端

### Task 3.1 新增 Migration 和 Repository

文件：

- `proseforge/infrastructure/database/migrations/versions/0008_model_usage.py`
- `proseforge/infrastructure/database/models/usage.py`
- `proseforge/infrastructure/database/repositories/usage.py`
- `proseforge/infrastructure/database/uow.py`
- 数据库集成测试

要求：

- [ ] 创建 `model_usage_records`。
- [ ] 扩展 Workflow 必要字段。
- [ ] 升级和降级可执行。
- [ ] 旧数据库数据不丢失。
- [ ] 幂等约束和索引生效。

提交：

```text
feat: add durable model usage persistence
```

### Task 3.2 实现 Usage Domain 和 Provider 归一化

文件：

- `proseforge/domain/usage/`
- `proseforge/providers/usage.py`
- 各 Provider adapter
- Provider contract tests

要求：

- [ ] 所有 Provider 输出统一 `UsageDelta`。
- [ ] 累计值不会重复相加。
- [ ] 真实和估算可区分。
- [ ] 没有用量字段时不会伪造真实用量。

提交：

```text
feat: normalize provider token usage events
```

### Task 3.3 接入聊天、写章、审校、改写

修改：

- `GenerateReply`
- `novel_generation.py`
- `celery_app.py`
- 工作流 checkpoint

要求：

- [ ] 每次模型调用产生记录。
- [ ] Writer、Editor、Rewriter 分用途统计。
- [ ] SSE 发布用量增量。
- [ ] 延迟和终态被记录。
- [ ] 失败调用保留已发生的实际用量。

提交：

```text
feat: record usage across chat and novel workflows
```

### Task 3.4 实现 Usage API 和预算限制

文件：

- `proseforge/application/usage/`
- `proseforge/api/routes/usage.py`
- `proseforge/api/main.py`
- `workflows.py`
- API 测试

要求：

- [ ] 完成第 5.6 节接口。
- [ ] 聚合查询有 owner/project 边界。
- [ ] Workflow 响应返回真实预算字段。
- [ ] 超限进入 `BUDGET_BLOCKED`。
- [ ] 提高限制后可继续。

提交：

```text
feat: expose usage summaries and enforce workflow budgets
```

## 阶段 4：实现 Token Web

### Task 4.1 实现 TokenMeter 和 Usage 页面

文件：

- `apps/web/src/features/usage/`
- `apps/web/src/pages/UsagePage.tsx`
- `apps/web/src/layouts/WorkspaceHeader.tsx`
- API contracts/tests

要求：

- [ ] 本次、会话、项目、工作流四层统计。
- [ ] 实际/估算视觉区分。
- [ ] 费用未知时不显示 0。
- [ ] 流式更新节流，不造成输入卡顿。
- [ ] 数值可访问，不能只靠颜色区分。

提交：

```text
feat: show real token usage throughout the web app
```

### Task 4.2 Context 预算可视化

- [ ] API 根据当前模型目录返回 context window。
- [ ] 预留 system、history、output Token。
- [ ] Context 页面显示单项和整体预算。
- [ ] 70%/85%/95% 阈值有明确文案。
- [ ] 上下文预算不混入历史实际消耗。

提交：

```text
feat: visualize model-aware context budgets
```

## 阶段 5：实现完整水墨视觉

### Task 5.1 设计 Token、纸纹和基础组件

修改：

- `styles/tokens.css`
- `styles/reset.css`
- `styles/shell.css`
- `components/ink/*`

要求：

- [ ] 使用第 4 节 Token。
- [ ] 纸纹不降低正文对比度。
- [ ] 朱砂占比克制。
- [ ] Focus、Hover、Disabled、Error 状态完整。
- [ ] 深色模式暂不实现，避免偏离黑白宣纸方向；保留未来 Token 能力。

提交：

```text
feat: establish the proseforge ink design system
```

### Task 5.2 重做 Shell、项目页和登录页

- [ ] 桌面/平板/手机布局符合第 4.3 节。
- [ ] API 离线有独立状态。
- [ ] 项目列表显示真实进度和 Token。
- [ ] 无项目空状态清晰。

提交：

```text
feat: rebuild the ink shell and project entry flows
```

### Task 5.3 重做写作工作台

- [ ] 中央正文优先。
- [ ] AI 助手平板/手机以 Drawer/Sheet 展示，不消失。
- [ ] 章节目录、版本历史、导出和 Token 状态统一。
- [ ] 保存冲突和草稿恢复有明确交互。
- [ ] 不引入大面积黑板或高饱和彩色卡片。

提交：

```text
feat: rebuild the writing studio as an ink manuscript workspace
```

### Task 5.4 重做 Context、Workflow、Settings、Usage

- [ ] 各页面使用统一水墨组件。
- [ ] 表格在手机端转换为可读列表。
- [ ] Workflow 时间线显示真实状态和用量。
- [ ] Settings 使用动态 Provider/Model。

提交：

```text
feat: finish ink views for context workflow settings and usage
```

## 阶段 6：修复小说产品逻辑

### Task 6.1 动态 Provider、模型和凭据

后端：

- [ ] 凭据按 `(user_id, provider)` 唯一 Upsert。
- [ ] 增加替换、删除接口。
- [ ] 保存后探测并同步模型。
- [ ] 模型列表按可用状态、能力和上下文窗口返回。

前端：

- [ ] Provider 从 API 读取。
- [ ] 模型可搜索选择。
- [ ] Writer/Editor 分开配置。
- [ ] 本地 Provider 明确提示地址安全规则。

提交：

```text
feat: make provider and model settings data driven
```

### Task 6.2 修复大纲问答和章节范围

- [ ] 前端按 `missing_questions` 逐项回答，不再写死第三人称、12 章和 1500 字。
- [ ] API 使用结构化 answers 合并现有大纲。
- [ ] Workflow 默认章节范围来自确认后的计划。
- [ ] 用户可选择起止章、Writer、Editor、Token/费用限制。

提交：

```text
feat: turn outline intake into a real planning flow
```

### Task 6.3 接入真实小说上下文

聊天 Prompt 至少包含：

- 当前项目基础信息。
- 当前章节标题和正文摘要/选区。
- 已确认大纲和章节计划。
- 置顶故事记忆。
- Context Compiler 选择的相关条目。
- 最近对话历史。
- 明确的 Token 预算和裁剪报告。

自动写章：

- [ ] Worker 编译 context snapshot。
- [ ] Writer/Editor 使用同一份可追踪 context hash。
- [ ] Prompt 超限时按优先级裁剪，并把裁剪结果展示给用户。
- [ ] 原始正文和故事记忆不因压缩被删除。

提交：

```text
feat: ground writing assistance in durable story context
```

### Task 6.4 完成本地化

- [ ] 所有用户可见字符串进入 i18n 字典。
- [ ] API 错误 code 与前端本地化文案分离。
- [ ] Provider/Model 原名不翻译。
- [ ] 状态枚举集中映射。
- [ ] 中文和英文分别运行 E2E。

提交：

```text
feat: complete bilingual user-facing localization
```

## 阶段 7：安全、部署和可观测性

### Task 7.1 会话与 Cookie

- [ ] `secure` 根据 production/public_url 自动启用。
- [ ] Cookie 配置 `httponly`、合理 `samesite`、明确过期时间。
- [ ] 对 Cookie 写接口实施 Origin/CSRF 校验。
- [ ] Redis 登录限速和失败退避。
- [ ] Logout 和 session expired 流程完整。

提交：

```text
security: harden session cookies and authentication flows
```

### Task 7.2 生产 Compose

- [ ] 新增 `compose.prod.yaml` 或等价生产覆盖文件。
- [ ] 数据库密码、JWT、Master Key 必须显式提供。
- [ ] 开发默认值只保留在 development。
- [ ] 增加升级、备份、恢复和回滚文档。
- [ ] 镜像和依赖使用明确版本。

提交：

```text
ops: add reproducible production compose configuration
```

### Task 7.3 安全日志和诊断

- [ ] 每个请求/任务有 correlation ID。
- [ ] 用量记录、工作流错误和 Provider 错误可关联。
- [ ] 日志不含正文、Prompt、Key。
- [ ] Web 提供可复制的脱敏诊断信息。

提交：

```text
ops: add privacy-safe diagnostics for web workflows
```

## 阶段 8：完整测试和发布门

### Task 8.1 后端测试

新增至少：

```text
tests/contract/providers/test_usage_normalization.py
tests/integration/database/test_model_usage.py
tests/integration/conversations/test_cancel_race.py
tests/integration/workflows/test_budget_enforcement.py
tests/api/test_usage.py
tests/api/test_workflow_requeue.py
```

覆盖：真实 usage、估算 fallback、重复事件去重、取消竞态、预算阻止、owner 边界、迁移升级、重试入队。

### Task 8.2 前端测试

新增至少：

```text
apps/web/src/features/editor/ChapterEditor.test.tsx
apps/web/src/features/usage/TokenMeter.test.tsx
apps/web/src/features/workflows/WorkflowStatus.test.tsx
apps/web/src/features/providers/ProviderSettings.test.tsx
apps/web/e2e/editor-draft-and-export.spec.ts
apps/web/e2e/token-usage.spec.ts
apps/web/e2e/workflow-control.spec.ts
apps/web/e2e/responsive-assistant.spec.ts
apps/web/e2e/localization.spec.ts
```

### Task 8.3 视觉与可访问性

- [ ] Playwright 保存 1440、1024、768、390 四组基准截图。
- [ ] 检查无横向溢出、文字截断和隐藏核心功能。
- [ ] 键盘可完成登录、项目、编辑、发送、停止、设置。
- [ ] Focus 清晰、对比度达标、状态不只靠颜色。
- [ ] `prefers-reduced-motion` 生效。

### Task 8.4 最终命令

依次执行，任一失败必须修复后重跑：

```bash
git diff --check

docker compose -f compose.yaml -f compose.test.yaml config --quiet
docker compose -f compose.yaml -f compose.test.yaml build

docker compose -f compose.yaml -f compose.test.yaml run --rm legacy-test
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test
docker compose -f compose.yaml -f compose.test.yaml run --rm contract-test
docker compose -f compose.yaml -f compose.test.yaml run --rm migration-test
docker compose -f compose.yaml -f compose.test.yaml run --rm recovery-test
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test

docker compose -f compose.yaml -f compose.test.yaml up -d --build postgres redis provider-mock api worker scheduler web
docker compose -f compose.yaml -f compose.test.yaml run --rm e2e

docker compose ps
docker compose exec api python -m proseforge.operations.startup_check
docker compose exec worker celery -A proseforge.workflows.celery_app inspect ping --timeout=5
```

再执行故障验证：

- [ ] 编辑时重启 Web，草稿保留。
- [ ] 生成时刷新浏览器，SSE 重连并继续显示 Token。
- [ ] 生成时点击停止，不再增加 Chunk 和 Token。
- [ ] 停止 Worker 后恢复，任务进入可恢复状态。
- [ ] 停止 Redis 后 readiness 503，恢复后 200。
- [ ] PostgreSQL `down/up` 后项目、用量、工作流、版本均保留。
- [ ] 从旧 0007 数据库升级到 0008，无数据丢失。
- [ ] 备份恢复后用量聚合与原库一致。

最终提交：

```text
release: complete proseforge web v1.1 ink and usage remediation
```

---

# 7. 禁止事项

- 禁止只在前端用字符长度模拟“实际 Token”并不加估算标签。
- 禁止把 Context 已用量当成 API 历史消耗。
- 禁止把所有厂商费用写死在代码中并长期不更新。
- 禁止继续把页面写成一行超长 JSX。
- 禁止为追求水墨风降低文字对比度。
- 禁止大面积黑色背景、霓虹渐变、玻璃卡片和过多圆角。
- 禁止在移动端隐藏 AI 助手、Token 或工作流控制。
- 禁止空 `catch` 吞掉错误。
- 禁止用固定 `setTimeout` 假装工作流完成。
- 禁止在 Worker 完成时无条件覆盖 `CANCELLED`。
- 禁止把模型 ID 和 Provider 永久硬编码在前端。
- 禁止删除旧章节、旧版本、旧大纲或旧 Context 来简化迁移。
- 禁止完成后只报告“测试通过”，必须列出真实数量和命令。

---

# 8. 最终交付物

完成后仓库至少包含：

1. 水墨视觉系统和全部重构后的 Web 页面。
2. Token/费用领域模型、Migration、Repository、API、SSE 和 Web 组件。
3. 修复后的编辑器、消息取消、工作流重试/继续/恢复。
4. 动态 Provider/模型/凭据管理。
5. 基于小说真实上下文的聊天和自动写章。
6. 完整中英文界面。
7. `compose.prod.yaml`、升级、备份、恢复和回滚说明。
8. 新增的单元、契约、集成、E2E、视觉和可访问性测试。
9. `artifacts/WEB_V1_1_FINAL_VALIDATION.md`，记录所有真实命令、退出码、测试数量、故障注入结果和仍存在的限制。
10. 更新 `README.md`、`CHANGELOG.md`、`VERSION`，版本建议提升为 `1.1.0`。

---

# 9. Kimi Code 最终执行指令

```text
读取 AGENTS.md 和 docs/plans/PROSEFORGE_WEB_V1_1_INK_USAGE_REMEDIATION_PLAN.md。
以最新 master 为基线创建 feat/web-v1.1-ink-usage。
严格按 Task 0.1 到 Task 8.4 顺序连续执行。
每个 Task 先写失败测试，再实现，再运行对应 Docker 测试，再提交。
不要按工作日拆分，不要为常规实现选择询问用户，不要跳过失败测试。
发现计划与真实代码不一致时，优先保持数据安全、架构边界、真实 Token 和普通用户可用性，并在最终验证报告说明调整。
完成全部阶段、全量测试、故障注入、备份恢复和视觉检查后，生成最终验证报告；未全部通过时不得宣称完成。
```
