# ProseForge V2 Chat Workspace 补齐实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 V1.5 的可用 Web 服务升级为 ChatGPT 式专业小说工作台：会话、分支、模型思考强度、Story Bible、编辑器、审稿、改稿、工作流和导出形成一个可解释的用户闭环，并达到蓝图 V2 Chat Workspace 的全部验收标准。

**Architecture:** 前端 app/features/lib 分层，TanStack Router 路由 + TanStack Query 服务端状态 + Zustand  ephemeral 状态 + Tiptap 编辑器 + React Flow 画布；后端延续 domain/application/api/infrastructure，聊天消息是不可变事件，AI 输出先成为 RevisionProposal，用户批准后才产生 ChapterVersion。模型能力以 provider/model catalog 为事实。

**Tech Stack:** React 19, TypeScript, Vite, TanStack Router/Query, Zustand, Tiptap/ProseMirror, React Flow (@xyflow/react), i18next, IndexedDB, Vitest, Playwright; FastAPI, Pydantic 2, SQLAlchemy 2, PostgreSQL/SQLite, SSE, Alembic.

**蓝图权威源（每个任务动手前必读对应文件）：** （下称 BLUEPRINT，原始蓝图未随公开仓库发布），以及同级的 `DESIGN_SYSTEM_INK.md`（视觉唯一基准）、`COMPETITIVE_ANALYSIS.md`（功能决策 §三）、`TEST_EXECUTION_POLICY.md`（测试节奏）。

## Global Constraints

- V2 只做 Web，不做 Electron/Flutter/原生移动端；保持 V1.5 native/server runtime ports，V1.5 已验证能力（原生包、web 子命令、升级）不得回退。
- 不把功能堆回 `apps/web/src/app/App.tsx` 的视图切换巨石；`main.tsx` 保持薄挂载（`main.structure.test.ts` 守护）。
- 全部 UI 以 `DESIGN_SYSTEM_INK.md` 为唯一视觉基准：墨分五色 token、朱砂仅印章、feature 目录零硬编码色值；V2-001 先修 `tokens.css` 失效变量并加 tokens 守护测试。
- 测试执行以 `TEST_EXECUTION_POLICY.md` 为准：**任务级不起容器**；L1 批次（B1=V2-001~003、B2=V2-004/005、B3=V2-006/007、B4=V2-008/009）在 Podman 内一次 up、串行 exec、down -v；全量 Playwright 只在 V2-010（L2）。
- 正式正文只能由用户批准的 revision proposal 产生新版本（覆盖 V2 聊天/编辑器/选区/审改表面；v1 `generate_novel` 自主写作工作流的直写行为是既有产品功能，本计划不重构，属显式 non-goal）。
- 消息和分支不可变；删除是归档，不是物理删除。
- 模型列表、context window、reasoning 参数来自 provider/model catalog；不支持项返回 422 / 灰显 + tooltip，禁静默降级。
- 支持中文/英文、键盘导航、窄屏 Web；**不以"页面存在"/401/#root 作为 E2E 证据**。
- 不在日志中记录 API key、完整 Prompt、完整正文；trace 使用 message_id、project_id 和哈希。
- V2 不创建/扩展 V3 Agent Team/Task Graph 运行时（迁移 0016–0024 与 `agent_runs` 路由已存在，本计划不触碰）。
- 迁移纪律：0001–0012、0016–0024 不可变；唯一允许的改动是把 `0016_agent_runs.py` 的 `down_revision` 从 `0012_review_revision` 改为 `0015_review_reports`（一行），新增 0013/0014/0015 插入其间。
- 仓库根有一个会话前遗留的未跟踪文件 `NUL`，会让根目录 ripgrep 报错——搜索时按子目录 grep，不要碰它。

## 现状基线（2026-07-18 四路盘点确认，执行者零上下文起点）

**后端（骨架已在，核心逻辑造假/缺失）：**
- `proseforge/workflows/tasks.py:343-403` `generate_chat`：加载了全部分支历史（L371）但只把**最近一条用户消息**发给模型（L395-399），`system_blocks=()`，无 ContextSnapshot、无 Story Bible、无 model/reasoning 参数。`resume` payload 键未被读取。
- 分支/编辑/再生成/比较已存在：`application/conversations/edit_message.py`（fork 新分支）、`regenerate_reply.py`（同分支追加 sibling，但 `generation_attempt` 从不递增）、`compare_branches.py`、`api/routes/branches.py`（/api/v2 全套）。消息列 `model_snapshot_json`/`reasoning_snapshot_json`/`content_hash`（迁移 0010）**没有任何代码写入**。
- SSE：`infrastructure/events/database.py` `DatabaseEventStream.subscribe` 是**一次性回放**（单查后发完即结束），无 live tail、无心跳；线上事件只有 `content.delta`/`usage.updated`，无 `message.started/completed/failed`。持久化先于广播（好）。`Last-Event-ID` 已支持。
- Story Bible：`domain/story_bible/entities.py`（含 promise kind）、`api/routes/story_bible.py`（GET/POST/pin）、表 `story_bible_entries`（迁移 0011）都在；**无 PATCH、无版本写入、无 promise 状态机、无 triggers、全系统零注入点**（grep 证实）。
- Context：`context_engine/`（compiler/budgeting/tokenizer/dedup/compaction）纯函数完备；`ContextSnapshotModel` 持久化但与 message/generation 无关联；`budgeting.calculate_budget` 无生产调用方；聊天从不消费快照；`api/routes/context.py:57` 硬编码 `context_window=128000`。
- 模型能力：`domain/model/capabilities.py`（ReasoningLevel 五级 + ModelCapabilities）、`application/models/reasoning_policy.py`、`api/routes/model_capabilities.py`（422 已支持）都在；但 `resolve_model` 无生产调用方，聊天路径从不读 catalog。
- 修订：`api/routes/revisions.py` create/list/approve/reject 在路由层（整文替换，无 hunks）；approve 有软幂等重放 + 409 stale 检查，但 `repositories/revision.py:18` `get_owned` 是**无锁 SELECT**，并发 approve 可建多版本；无 review_reports 表（`QualityReportModel` 是死表）；无 `reviews.py` 路由。
- 工作流：v1 novel 工作流有 pause/resume/cancel/retry + checkpoint JSON + BUDGET_BLOCKED；无 definitions CRUD、无节点级状态表、控制无幂等键、`WorkflowStepModel` 死表、事件 SSE 一次性回放。
- 导出：`api/routes/exports.py` txt/md/json/docx/epub，支持 version_ids；**manifest 只塞响应头 `x-proseforge-manifest`，不落库**；无模板预设。
- 迁移链：0001→…→0012→0016→…→0024 单 head（0024）。0013/0014/0015 缺。
- 用量：`ModelUsageRecordModel`（0008）scope 齐全（user/project/conversation/message/workflow/step）、`reasoning_tokens`/`usage_source` 齐；聊天只写 message+conversation scope。

**前端（真实应用是 App.tsx，features 全是未挂载壳）：**
- `apps/web/src/app/App.tsx`（141 行）：`useState<View>` 本地切换（:134），Studio/Workflow/Agent/Settings/Context 全内联；`main.tsx` 7 行薄挂载。`app/router.ts`（手写 pushState 路由）与 `app/query.tsx`（QueryClient）是死代码。
- 依赖：有 `@tanstack/react-query 5.101.2`、`@axe-core/playwright`；**无** @tanstack/react-router、zustand、@tiptap/*、@xyflow/react、i18next、react-markdown、dompurify、react-hook-form、zod。
- `features/chat/*` 纯展示壳（只被测试引用）；真聊天在 `App.tsx:60`（SSE 只订 `content.delta`，轮询收尾）。`features/editor/ManuscriptEditor.tsx:10` 是 `<textarea>`；`features/workflows/WorkflowCanvas.tsx:2` 是按钮列表；`features/story-bible|review|revision|context` 目录不存在。
- `styles/tokens.css`：`--border` 被 `views.css:5` 引用但**未定义**；无 `[data-theme="rubbing"]` 暗色主题；`components/ink/Ink.tsx` 六个组件的 class **无任何 CSS 规则**；`App.tsx` 挂载的 `.shell/.rail/.nav` 也无 CSS。
- `lib/api/client.ts`（85 行）：~45 个类型化端点 + `subscribeConversationEvents`（只订 content.delta）；`lib/drafts.ts`：IndexedDB 草稿（`chapter:{pid}:{cid}` 与 `{conv}:{branch}:chat` 两套 key）；`features/chat/chatStore.ts` 另用 localStorage（需统一到 IDB）。
- i18n：`lib/i18n.tsx` 手写 5-key 模块，无人 import；UI 文案硬编码英文。
- PWA：`public/sw.js`（静态资源 cache-first，已拒 /api/）、`manifest.webmanifest` 存在但 `index.html` **未 link**、icons 为空。
- e2e（apps/web/e2e，12 个 spec）：`ordinary-user-smoke` 被 `test.skip(true, "post-reload workspace restoration is V2-001 scope")` 跳过（V2-001 必须解除）；`v2-professional-flow.spec.ts` 是 API 级（需升级为真实 10 步）；`visual-a11y.spec.ts` axe 已在跑（需扩到 4 页 0 critical/serious）；v3 两个 spec 保持跳过（V3 阶段恢复）。

**测试基建：**
- `tests/api/` 全部是未认证 401 门；**没有认证 fixture**——本计划在 V2-002 建 `tests/api/conftest.py`。
- `tests/integration/` 有 conversations/context/database/workflows，缺 `revision/`。
- 迁移链完整性测试不存在（只有 sqlite upgrade-head 冒烟 `tests/database/test_sqlite_bootstrap.py:70`）。
- compose.test.yaml 服务齐全（api-test/web-test/e2e/migration-test/recovery-test/contract-test），V1.5 已验证可用。
- 环境备忘（每次 Bash 调用都需）：`export PATH="$PWD/.tools:$PATH"`（compose provider）+ `unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy`；`podman run` 临时容器需显式 `-e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY=`；HF 相关测试需 `-e HF_ENDPOINT=https://hf-mirror.com`。

## 批次与验证地图（TEST_EXECUTION_POLICY §四 V2 表）

| 批 | 任务 | 定向验证（Podman 一次 up 串行 exec down -v） | 证据文档 |
|---|---|---|---|
| B1 | V2-001/002/003 | web-test（typecheck+unit+build）；api-test 跑 tests/api/test_conversation_branches.py tests/integration/conversations + 回归 tests/api；ruff | artifacts/BATCH_VALIDATION_V2_B1.md |
| B2 | V2-004/005 | api-test 跑 tests/api/test_model_capabilities.py tests/api/test_story_bible.py tests/integration/context tests/contract/providers；web-test | artifacts/BATCH_VALIDATION_V2_B2.md |
| B3 | V2-006/007 | api-test 跑 tests/api/test_reviews_revisions.py tests/integration/revision + 回归 tests/api；web-test | artifacts/BATCH_VALIDATION_V2_B3.md |
| B4 | V2-008/009 | api-test 跑 tests/api/test_workflow_definitions.py tests/integration/workflows tests/api/test_sse_reconnect.py；migration-test；web-test；e2e --grep "professional\|a11y" | artifacts/BATCH_VALIDATION_V2_B4.md |
| L2 | V2-010 | 全矩阵 legacy/api/contract/migration/recovery/web/e2e + ruff；生成 v2-openapi.json / v2-schema-check.txt | artifacts/V2_FINAL_VALIDATION.md |

每批证据文档必须含：任务清单、每条命令、退出码、测试计数、`down -v` 确认。Podman 操作前置：`export PATH="$PWD/.tools:$PATH" && unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy`。

## 跨任务接口契约（先锁定，各任务不得偏离）

```python
# V2-002 产出，V2-005 升级，聊天与（V2-008）工作流共用
# proseforge/application/conversations/compile_chat_context.py
@dataclass(frozen=True)
class ChatContext:
    system_blocks: tuple[dict, ...]          # persona + pinned/triggered facts + omitted 摘要
    messages: tuple[dict, ...]               # 全分支历史裁剪后 [{"role","text"},...]
    snapshot_id: str                          # 已持久化 ContextSnapshot id
    injected_fact_ids: tuple[str, ...]        # 本次注入的 story bible 条目 id
    model_snapshot: dict                      # {provider,model,context_window,max_output_tokens,source}
    reasoning_snapshot: dict                  # {level,parameter,strength} 或 {level,supported:False,reason}

# V2-002 产出，V2-008 复用：SSE live tail（infrastructure/events/database.py）
# subscribe(topic, after_id) -> AsyncIterator[event]，语义 = 回放(after_id 之后)
#   → 轮询新增(1s) → 直到 terminal 事件或对端断开；路由层每 15s 发 ": heartbeat" 注释帧。
TERMINAL_EVENTS = {"message.completed", "message.failed"}

# V2-002 产出，全部后续 API 测试共用：tests/api/conftest.py
# fixture client(TestClient, 建 schema) / auth_client(已 setup+登录 cookie, Origin 头注入)

# V2-006 产出，V2-007 消费：selection action -> proposal_id（绝不直接写正文）
# V2-007 产出：approve 事务契约（FOR UPDATE + 幂等重放 + 409 REVISION_BASE_CONFLICT）
```

---

### Task V2-001: 专业工作台外壳与文件拆解（拆 App.tsx）

**Blueprint ref（动手前必读）:** `BLUEPRINT/01_CHAT_SHELL_AND_APP_FILES.md`（布局契约/视觉规格/路由表）、`DESIGN_SYSTEM_INK.md` §2.2（tokens 修复）、§4.1（聊天视觉）。

**Files:**
- Create: `apps/web/src/app/providers.tsx`（QueryClientProvider + I18nProvider + RouterProvider 组合）、`apps/web/src/app/router.tsx`（TanStack Router 实例）、`apps/web/src/app/routes.tsx`（路由树）、`apps/web/src/styles/tokens.test.ts`（tokens 守护）
- Rewrite: `apps/web/src/app/App.tsx`（只留 providers+router 挂载，删掉 8 个内联视图与 `useState<View>`）
- Fill shells: `apps/web/src/components/layout/Sidebar.tsx`、`TopBar.tsx`、`WorkspaceSplit.tsx`
- Make real: `apps/web/src/features/chat/ChatPage.tsx`、`ChatComposer.tsx`、`MessageList.tsx`、`MessageCard.tsx`、`chatQueries.ts`（新建，TanStack Query hooks）、`chatStore.ts`（改 Zustand，ephemeral：inspector 开合、streaming 状态、命令面板）
- Modify: `apps/web/src/main.tsx`（挂 providers）、`apps/web/src/lib/api/client.ts`（SSE 订全事件词汇）、`apps/web/src/lib/drafts.ts`（聊天草稿 key 统一 `{conversationId}:{branchId}:chat`，删 chatStore 的 localStorage 实现）、`apps/web/src/styles/tokens.css`（补 `--border`）、`apps/web/src/styles/views.css`（删失效 var 引用矛盾）、`apps/web/src/styles/chat-shell.css`（三栏/卡片/印章/Ink 类真实规则）
- Delete: `apps/web/src/app/App.tsx` 旧视图切换 JSX（旧 Studio 内联聊天移到 features/chat）；`apps/web/src/app/router.ts`（手写路由，被 TanStack 取代，连带改写 `app/router.test.ts`）
- Deps（package.json + lockfile 同提交）: `@tanstack/react-router`、`zustand`、`react-markdown`、`dompurify` + `@types/dompurify`

**Interfaces:**
- Consumes: 现有 `lib/api/client.ts` 端点（conversations/messages/fork）、`lib/drafts.ts`（`saveDraft/loadDraft`）
- Produces: `chatQueries.ts` 导出 `useMessages(conversationId, branchId)`、`useSendMessage()`、`useStopMessage()`、`useRetryMessage()`；`routes.tsx` 路由表（下）；tokens 守护测试供全部后续 feature 复用

**路由表（blueprint §Routing）：** `/projects`、`/projects/$projectId/chat/$conversationId/$branchId`、`/projects/$projectId/manuscript/$chapterId`、`/projects/$projectId/review/$reportId`、`/projects/$projectId/workflows/$workflowId`、`/settings/models`。URL 不放正文。V1 内联视图（outline/context/usage/agents/settings）先以简单 route 页面承接原组件，避免本任务重写无关视图。

**布局契约：** Desktop `sidebar 264px | chat minmax(480px,1fr) | inspector 320px`；Tablet 侧栏折叠 + inspector drawer；Mobile 顶栏 + 消息流 + 固定 composer。配色：侧栏 `--paper`、主区 `--paper-raised`、检查栏 `--paper` + 左 1px `--ink-faint`。

**关键实现点：**
- Composer：Enter 发送 / Shift+Enter 换行 / Ctrl+K 命令面板；附件 .txt/.md/.docx 单文件 ≤2MB（超限提示）；model picker + reasoning picker（先接 `/api/v2/models`，V2-004 做真）；stop/retry；分支指示器；`aria-live="polite"` 流式播报；草稿存 IDB。
- 消息渲染：react-markdown + DOMPurify 白名单（禁 HTML 注入）；代码块 `--font-mono` + 复制钮；流式中 `--ink-mid` + 句末 4px 朱砂圆点光标，完成沉 `--ink`；失败=淡墨方章"止"+重试；用户消息右对齐淡墨描边卡；AI 消息无边信笺 `--font-prose` 17px/1.9；轮次间 BrushDivider。
- 分支切换器 `‹ 2/3 ›` 在消息右上（`--font-mono` 12px `--ink-light`），先读现有 fork 数据。
- 空态 EmptyScroll（"落笔即是开篇" + 示例提示）；错误态墨线卡 + 错误码 + 重试，无堆栈。
- tokens.css：补 `--border: var(--ink-faint)`；tokens 守护测试 = 扫描 `styles/*.css` 已定义变量集合 vs `src/**/*.tsx,css` 引用集合（零未定义）+ feature 目录零硬编码色值（正则 `#[0-9a-fA-F]{3,8}|rgb\(` 扫描，白名单 tokens.css）。
- `ordinary-user-smoke.spec.ts`：删 `test.skip(...)`（第 7 行），恢复刷新后工作区恢复断言（路由 + project 持久化使之为真）；spec 顶部注释更新为已恢复。

**Steps:**
- [ ] 1. 更新依赖：`package.json` 加 4 个依赖后重建 lockfile——`podman run --rm -v .:/app -w /app/apps/web -v proseforge_web-pnpm-store:/pnpm-store -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= node:22-alpine sh -lc "corepack enable && pnpm install --store-dir /pnpm-store"`（web-test 用 `--frozen-lockfile`，lockfile 必须先入库）。
- [ ] 2. 写失败测试：`ChatPage.test.tsx`（fixture 渲染 user/assistant/failed 三态、Enter 提交一次、Shift+Enter 换行、窄屏 drawer class、stop 钮、重试保留已生成部分、流式朱砂光标存在、分支计数器文本、附件 >2MB 拒绝提示、IDB 草稿恢复）、`tokens.test.ts`（守护）、`router.test.ts`（路由表解析）。
- [ ] 3. 实现 providers/router/routes + layout 三件套 + chat feature 真实化 + CSS（Ink 组件类、`.shell` 系列、三栏）。
- [ ] 4. 迁移旧视图到路由页（outline/context/usage/agents/settings 原样搬迁，不重构）。
- [ ] 5. 删 App.tsx 旧切换与死代码 `app/router.ts`；`main.structure.test.ts` 保持绿。
- [ ] 6. 本地静态：`git diff --check`。验证留 B1 批次。
- [ ] 7. Commit: `feat(web): add professional workspace shell`

### Task V2-002: 会话消息模型真实化 + 聊天上下文修复（核心）

**Blueprint ref:** `BLUEPRINT/02_CONVERSATION_AND_BRANCH_SYSTEM.md`（数据/API/测试）、`08_DATA_API_CONTRACTS.md`（错误格式/不变量）。

**本任务修掉用户排查的头号 V2 问题：`generate_chat` 不用上下文。**

**Files:**
- Create: `proseforge/application/conversations/compile_chat_context.py`、`tests/api/conftest.py`（认证 fixture，全部后续任务复用）、`tests/api/test_conversation_branches.py`、`tests/integration/conversations/test_history_sent_to_provider.py`
- Modify: `proseforge/workflows/tasks.py:343-403`（generate_chat 重写提示构建）、`proseforge/application/conversations/generate_reply.py`（发 message.started/completed/failed 事件；content_hash 落库）、`proseforge/application/conversations/send_message.py`（透传 reasoning_level）、`proseforge/api/routes/conversations.py` 与 `branches.py`（MessageRequest 加 `reasoning_level: str = "auto"`；v2 422 校验）、`proseforge/infrastructure/events/database.py` + `memory.py`（subscribe 改 replay+live tail）、`proseforge/infrastructure/database/repositories/conversation.py`（写 model/reasoning snapshot 与 content_hash）、`proseforge/domain/conversation/entity.py`（Message 加三字段）

**Interfaces:**
- Produces: 上文"跨任务接口契约"的 `ChatContext`、SSE live tail、auth fixture。
- `compile_chat_context` V2-002 版注入：system persona + 项目大纲摘要 + pinned story facts + 分支历史裁剪（按 `capabilities.context_window − output_reserve − 10%` 预算，最旧非 pinned 先丢，omitted 计入快照）；V2-005 升级为 trigger 注入。

**generate_chat 新核心（替换 tasks.py:371,395-399）：**
```python
visible = await uow.conversations.list_visible_messages(message.branch_id)
catalog = await uow.model_catalog.get(provider, model)          # catalog 为事实
capabilities = capabilities_from_model(catalog)
resolution = resolve_reasoning(reasoning_level, capabilities)   # 不支持已在路由层 422
context = await CompileChatContext(uow).execute(
    project_id=project_id, history=visible, capabilities=capabilities)
request = GenerationRequest(
    model=model,
    system_blocks=context.system_blocks,
    input_blocks=context.messages,
    max_output_tokens=capabilities.max_output_tokens,
    reasoning=resolution.get("parameter"),
)
# 落库：message.model_snapshot_json=context.model_snapshot
#       message.reasoning_snapshot_json=context.reasoning_snapshot
#       message 关联 context.snapshot_id（存进 model_snapshot_json["context_snapshot_id"]，不动 schema）
```
`ContextSnapshotModel` 每次生成持久化一条（payload 含 blocks + injected ids + omitted reasons），不可变。完成时 `content_hash = sha256(content)` 写回。

**SSE live tail（database.py + memory.py 同步改）：**
```python
async def subscribe(self, topic, after_id=None):
    last = int(after_id or "0")
    while True:
        rows = await self._fetch_after(topic, last)     # 单查 event_sequence > last
        for row in rows:
            last = row.event_sequence
            yield {"id": str(last), "event": row.event_type, **row.payload}
            if row.event_type in TERMINAL_EVENTS:
                return
        if not rows:
            await asyncio.sleep(1.0)                     # 轮询；路由层 15s 心跳注释帧
```
`generate_reply` 在 STREAMING 前 publish `message.started`，成功 publish `message.completed`，异常 publish `message.failed`（先持久化后广播，沿用现有顺序）；`conversations.py:149` 路由加 15s 心跳注释帧与断连清理。PARTIAL 续写逻辑（L396-398）保留。

**tests/api/conftest.py（认证 fixture）：**
```python
@pytest.fixture(scope="session")
def api_settings(tmp_path_factory):
    return Settings(
        database_url=os.environ["PROSEFORGE_TEST_DATABASE_URL"],
        public_url="http://testserver",
        blob_root=str(tmp_path_factory.mktemp("blobs")),
        backup_root=str(tmp_path_factory.mktemp("backups")),
    )

@pytest.fixture(scope="session")
def client(api_settings):
    sync_url = os.environ["PROSEFORGE_SYNC_DATABASE_URL"]
    alembic_cfg = Config("alembic.ini"); alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
    command.upgrade(alembic_cfg, "head")                 # 顺便验证迁移链
    yield TestClient(create_app(api_settings))

@pytest.fixture()
def auth_client(client, api_settings):
    r = client.post("/api/v1/auth/setup", json={"email": "t@example.local", "password": "twelve-char-pw"})
    assert r.status_code in (201, 409)                   # 会话内只首个用例 201
    return client                                        # cookie 已在 TestClient 内
```
mutation 需带 `headers={"Origin": "http://testserver"}`（`require_same_origin`）；fixture 内统一封装 `auth_client.post_json(...)` 助手。每用例独立数据用唯一 project 名隔离（共享会话库，不做 truncate——与 e2e 共享账号教训一致）。

**测试（写失败测试先行）：**
- `test_conversation_branches.py`：编辑原文不变（GET 原消息内容一致）、新分支恰一条 parent 边、跨用户 404、重复 `client_request_id` 幂等（同 message id）、重连不重复 delta（Last-Event-ID）、regenerate 两候选都在。
- `test_history_sent_to_provider.py`：spy provider 断言收到**全分支历史**（≥3 条，含 fork 前祖先）+ 非空 system_blocks + `max_output_tokens`=catalog 值；快照行已持久化且含 injected/omitted；message 三个 snapshot 字段已写。
- `tests/api/test_sse_reconnect.py` 扩展：subscribe 在 replay 后能收到新发布事件（live tail）、terminal 事件后流结束。
- 422：不支持的 reasoning_level 在 v2 send 即 422 且响应列支持级别。

**Steps:**
- [ ] 1. 写 `tests/api/conftest.py` + 上述失败测试。
- [ ] 2. 实现 compile_chat_context（V2-002 版）+ generate_chat 改造 + snapshot 字段落库。
- [ ] 3. 实现 SSE live tail + message.started/completed/failed + 路由心跳。
- [ ] 4. reasoning_level 透传 + 路由 422 校验。
- [ ] 5. `git diff --check`；Commit: `feat(chat): add immutable conversation messages`

### Task V2-003: 分支树与 fork 语义补全

**Blueprint ref:** `BLUEPRINT/02_CONVERSATION_AND_BRANCH_SYSTEM.md`、`01` §视觉（分支切换器）。

**Files:**
- Modify: `proseforge/application/conversations/regenerate_reply.py`（`generation_attempt = 同 parent 已有 assistant 数 + 1`）、`proseforge/application/conversations/compare_branches.py`（补逐条 diff：消息级 content 差异标记，不只计数）、`proseforge/api/routes/branches.py`（tree 响应带 `generation_attempt`/`parent_message_id`；compare 返回消息级 diff）
- Create: `apps/web/src/features/branches/BranchTreeView.tsx`（分支树：父子边、归档态）、`BranchCompareView.tsx`（左右栏消息对照，共同前缀灰显，分叉后逐条对照）、`apps/web/src/features/chat/MessageActions.tsx`（编辑旧消息 / 再生成 / 候选切换 ‹n/m›）
- Modify: `apps/web/src/features/chat/ChatPage.tsx`（接 MessageActions、分支切换器接通 tree/compare）、`apps/web/src/features/chat/chatQueries.ts`（useBranchTree/useCompare/useEditMessage/useRegenerate）

**语义（蓝图不变量）：** 编辑旧消息 = 在源消息处 fork 新分支（已实现，分支名可传）；regenerate = 同分支新 assistant 候选（保留旧候选），`generation_attempt` 递增；归档分支 owner 可查但默认导航隐藏（`list_branches` 默认过滤 ARCHIVED，加 `?include_archived=true`）。

**测试：**
- API：编辑后原分支消息流不变、新分支 parent 边指向 fork 消息；regenerate 产生两个候选且 attempt 1/2；compare 返回共同前缀 + 两侧尾部 diff；归档后默认列表不可见、`include_archived` 可见；跨用户 404。
- 前端：编辑按钮弹出编辑框 → 提交后出现分支指示 `‹ 2/3 ›`；候选切换翻页；compare 视图左右栏渲染。

**Steps:**
- [ ] 1. 写失败测试（API + vitest）。
- [ ] 2. 实现后端 attempt 递增 / compare diff / include_archived。
- [ ] 3. 实现 BranchTree/BranchCompare/MessageActions 并接入 ChatPage。
- [ ] 4. `git diff --check`；Commit: `feat(chat): add branch tree and fork semantics`

**B1 批次验证（三任务全绿后一次执行）：**
```bash
export PATH="$PWD/.tools:$PATH" && unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
podman compose -f compose.yaml -f compose.test.yaml up -d --build --parallel 1 postgres redis
podman compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest -q tests/api/test_conversation_branches.py tests/api/test_sse_reconnect.py tests/integration/conversations
podman compose -f compose.yaml -f compose.test.yaml run --rm api-test pytest -q tests/api
podman compose -f compose.yaml -f compose.test.yaml run --rm api-test ruff check proseforge tests
podman compose -f compose.yaml -f compose.test.yaml run --rm web-test
podman compose -f compose.yaml -f compose.test.yaml down -v
```
写 `artifacts/BATCH_VALIDATION_V2_B1.md`（命令/退出码/计数/down -v 确认）。

### Task V2-004: 模型目录与思考强度真实化

**Blueprint ref:** `BLUEPRINT/03_MODEL_CAPABILITIES_AND_REASONING.md`。

**Files:**
- Modify: `proseforge/api/routes/model_capabilities.py`（validate 响应补 `warnings`（脱敏）、`probe=false` 默认不呼 provider）、`proseforge/application/models/resolve_model.py`（接入 send/generate 路径：user override → provider response → checked catalog → conservative fallback 优先级）、`proseforge/context_engine/budgeting.py` 或新 `application/models/context_window.py`（`resolve_context_window(model_snapshot, conservative_floor=8192)`，替换 `api/routes/context.py:57` 硬编码 128000 与 `workflows/tasks.py:79` 硬编码 `input_budget=8000`）
- Create: `apps/web/src/features/models/ModelPicker.tsx`（真：catalog 分组、context window 显示）、`ReasoningPicker.tsx`（五级墨阶圆点 ●●●○○；不支持项灰显 + tooltip 原因；禁静默降级）、`modelCapabilities.ts`（query hooks）、`SettingsModelsPage.tsx`（/settings/models 路由页）
- Modify: `apps/web/src/features/chat/ChatComposer.tsx`（接真 picker，选择随消息发送）
- Tests: `tests/api/test_model_capabilities.py`、`tests/contract/providers/test_reasoning_mapping.py`、`apps/web/src/features/models/ModelPicker.test.tsx`（扩）

**契约（已有，扩用）：** `ReasoningLevel{AUTO,FAST,STANDARD,DEEP,MAX}`；不支持级别 → 422 + 支持列表；validate 返回 `{normalized_level, provider_parameter, context_window, warnings}`；usage 记录区分 `provider|estimated|missing`（`usage_source` 已有，补 `missing` 分支：provider 未返回 usage 时 source=missing 而非 estimated）。

**测试：** 不支持 MAX → 422；provider 特定映射（OpenAI reasoning_effort / Anthropic thinking / Google thinking_budget，按 catalog `reasoning_parameter`）；catalog context window 生效（替换两处硬编码后有测试钉住）；输出预留参与预算；逐消息切模型（同分支两条消息不同 model_snapshot）；warnings 不含密钥/URL 凭据。

**Steps:**
- [ ] 1. 写失败测试（api + contract + vitest）。
- [ ] 2. 实现 resolve_context_window 替换硬编码、validate warnings、usage missing 分支。
- [ ] 3. 实现两个 picker + settings 页并接 composer。
- [ ] 4. `git diff --check`；Commit: `feat(models): expose truthful reasoning capabilities`

### Task V2-005: Story Bible 结构化 + 触发注入 + Context Inspector

**Blueprint ref:** `BLUEPRINT/04_STORY_BIBLE_AND_MANUSCRIPT_EDITOR.md` 前半（数据模型/借鉴落点/API/测试）。

**Files:**
- Modify: `proseforge/domain/story_bible/entities.py`（value 结构约定：character 含 `voice{sentence_len:[min,max],connectors:[],banned_words:[],emotion_baseline,register}`；全 kind 支持 `triggers:[]` 与 `budget_tokens:int`；promise 状态机 `open|developing|resolved|abandoned` + 合法迁移表）、`proseforge/api/routes/story_bible.py`（新增 `PATCH /story-bible/{entry_id}`（版本 +1，optimistic version 冲突 409）、`POST /story-bible/{entry_id}/status`（promise 非法迁移 422）、`POST /projects/{id}/context/preview`）
- Create: `proseforge/application/story_bible/service.py`（`StoryBibleService`：crud + 版本 + 状态机校验 + `match_triggers`）、`proseforge/application/context/build_snapshot.py`（`BuildContextSnapshot`：ordered blocks{source_type,source_id,token_estimate,priority,pinned,redaction} + injected ids + 命中原因 + omitted reasons）、`apps/web/src/features/story-bible/StoryBiblePage.tsx`、`FactEditor.tsx`（kind 表单：voice 子结构、triggers 编辑器、promise 状态章）、`apps/web/src/features/context/ContextInspector.tsx`（included/omitted blocks + token 预算 + "这一段生成用了哪些设定"按消息快照回溯）
- Modify: `proseforge/application/conversations/compile_chat_context.py`（升级为 trigger 注入：未命中条目不占 token；pinned 常驻；快照记录 injected_fact_ids + reasons）、`apps/web/src/features/chat/ChatPage.tsx`（inspector 栏接 ContextInspector）

**触发匹配（核心逻辑）：**
```python
def match_triggers(entries: list[StoryFact], text: str) -> list[TriggeredFact]:
    hits = []
    for entry in entries:
        triggers = entry.value.get("triggers") or [entry.key]
        matched = [t for t in triggers if t and t in text]
        if entry.pinned:
            hits.append(TriggeredFact(entry, reason="pinned"))
        elif matched:
            hits.append(TriggeredFact(entry, reason=f"trigger:{matched[0]}"))
    return hits   # 未命中 → 不进 block、不占 token
```
匹配文本 = 当前指令 + 最近 N 条历史 + 编辑器选区（如有）；预算超出时 pinned 优先、低 confidence 先丢，omitted 记原因。

**promise 状态机：** `open → developing → resolved|abandoned`；`resolved/abandoned` 终态；非法迁移（如 resolved→open）422 + `details.allowed`。

**测试：**
- API：条目 ownership（跨用户 404）；PATCH version 冲突 409；pin 在预算压力下保留（构造小 context window）；trigger 命中才注入（未命中条目不出现在快照 blocks、token 估算不含）；快照含 injected id 清单与命中原因；promise 非法迁移 422。
- 集成 `tests/integration/context/test_snapshot_pins_facts.py`（新建）：同输入两次快照 hash 一致（可复现）；快照持久化后与消息关联可查。
- 前端 vitest：FactEditor 保存 voice/triggers；Inspector 渲染 included/omitted。

**Steps:**
- [ ] 1. 写失败测试。
- [ ] 2. 实现 story_bible service + 路由扩展 + 状态机。
- [ ] 3. 实现 build_snapshot + compile_chat_context trigger 升级 + preview 端点。
- [ ] 4. 实现 StoryBiblePage/FactEditor/ContextInspector。
- [ ] 5. `git diff --check`；Commit: `feat(context): add structured story bible`

**B2 批次验证：**
```bash
podman compose -f compose.yaml -f compose.test.yaml up -d --build --parallel 1 postgres redis
podman compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest -q tests/api/test_model_capabilities.py tests/api/test_story_bible.py tests/integration/context tests/contract/providers
podman compose -f compose.yaml -f compose.test.yaml run --rm api-test pytest -q tests/api
podman compose -f compose.yaml -f compose.test.yaml run --rm api-test ruff check proseforge tests
podman compose -f compose.yaml -f compose.test.yaml run --rm web-test
podman compose -f compose.yaml -f compose.test.yaml down -v
```
写 `artifacts/BATCH_VALIDATION_V2_B2.md`。

### Task V2-006: Tiptap 专业编辑器与选区动作

**Blueprint ref:** `BLUEPRINT/04_STORY_BIBLE_AND_MANUSCRIPT_EDITOR.md` 后半（Editor 动作表/章节树）、`DESIGN_SYSTEM_INK.md` §4.2。

**Files:**
- Deps: `@tiptap/react`、`@tiptap/starter-kit`、`@tiptap/core`（lockfile 同提交，命令同 V2-001 步骤 1）
- Rewrite: `apps/web/src/features/editor/ManuscriptEditor.tsx`（Tiptap 替换 textarea；保留纯文本 adapter 作 fallback：`?editor=plain` 或 Tiptap 初始化异常时降级并记 console.warn）
- Create: `apps/web/src/features/editor/SelectionToolbar.tsx`（真：六动作）、`ChapterTree.tsx`（拖拽排序 + StatusStamp 三态）、`apps/web/src/features/editor/ManuscriptPage.tsx`（/manuscript/:chapterId 路由页）
- Modify: `apps/web/src/features/editor/editorState.ts`（选区坐标、base_version_id、optimistic version 保存、脏草稿恢复）
- Backend Create: `proseforge/application/writing/selection_action.py`（选区动作 → 流式生成 → 每候选一个 proposal，**绝不直接写正文**）
- Backend Modify: `proseforge/api/routes/chapters.py`（`POST /api/v2/chapters/{chapter_id}/selection-actions`：body `{action, from, to, selected_text_hash, base_version_id, params}`；hash 不匹配当前选区 → 409；返回 `{proposal_id}` 或 `{candidate_proposal_ids:[...]}`）

**选区动作表（blueprint 04）：** continue（candidates 1..3，候选并排签页）/ expand（ratio 1.5|2|3）/ shorten（0.5|0.7）/ rewrite（instruction?）/ change-tone（register 来自 `packs/voice/registers` + sensory 五感子参数）/ review（产 ReviewReport 面板，非 diff——V2-007 落地报告结构）。动作请求带 `chapter_id, base_version_id, from, to, selected_text_hash, action`；提案内联 diff 预览（原文淡墨删除线 + 新文焦墨）；键盘 `A` 批准 / `R` 退回（批准逻辑属 V2-007）。

**编辑器规格：** 草稿 IDB 持久化 + optimistic version 保存（冲突 409 提示）；章节树拖拽排序（拖起缩放 0.96 + 晕染影）+ StatusStamp（草稿=淡墨朱文/修改中=浓墨/定稿=朱砂白文）；稿纸格线可开关；章节标题 `--font-seal`。

**测试：** 已知文档选区坐标（Tiptap doc 位置 → from/to）；脏草稿刷新恢复；optimistic 冲突 409；动作返回 proposal_id 且正文未变（GET chapter 内容不变 + proposals 列表 +1）；continue 多候选数量正确；fallback adapter 可用。

**Steps:**
- [ ] 1. 加依赖重建 lockfile。
- [ ] 2. 写失败测试（vitest + api）。
- [ ] 3. 实现 Tiptap 编辑器 + 选区工具条 + 章节树 + 后端 selection_action。
- [ ] 4. `git diff --check`；Commit: `feat(editor): add selection-aware manuscript actions`

### Task V2-007: 审稿/改稿提案引擎（含并发修复）

**Blueprint ref:** `BLUEPRINT/05_REVIEW_REVISION_ENGINE.md`（状态机/API/测试）、`08` 错误格式。

**本任务修掉用户排查的并发问题：approve 无锁无幂等。**

**Files:**
- Migration Create: `proseforge/infrastructure/database/migrations/versions/0015_review_reports.py`（编号见"迁移纪律"）：
  - `review_reports` 表：`id, project_id(idx), scope, subject_type, subject_id, findings_json, scores_json, model_snapshot_json, context_snapshot_id, usage_call_id, status, created_at`
  - `revision_proposals` 加列（batch_alter_table 保 sqlite 可 downgrade）：`hunks_json`（patch 操作列表）、`affected_facts_json`、`conflict_status`、`guard_status`、`context_snapshot_id`、`idempotency_key`（unique, nullable）、`decided_at`、`updated_at`
  - `0016_agent_runs.py` 的 `down_revision` 改为 `"0015_review_reports"`（唯一允许的旧文件改动）
- Modify: `proseforge/domain/revision/proposal.py`（hunks/patch 操作 + 全状态机 `DRAFT→GENERATED→REVIEWED→PROPOSED→APPROVED→VERSION_CREATED / REJECTED / EXPIRED`）、`proseforge/domain/review/report.py`（severity `blocking|suggestion|nit`、evidence ranges、model/context 快照、usage 关联）
- Rewrite: `proseforge/application/revision/create_proposal.py`、`approve_proposal.py`、`reject_proposal.py`（从纯函数升级为 UoW 事务用例）
- Create: `proseforge/application/quality/create_review.py`、`proseforge/api/routes/reviews.py`（`POST /api/v2/projects/{id}/reviews`、`GET /api/v2/reviews/{id}`）
- Modify: `proseforge/api/routes/revisions.py`（hunks 创建、`GET /api/v2/proposals/{id}/diff`、approve 走用例）、`proseforge/infrastructure/database/repositories/revision.py`（`get_owned_for_update`：`SELECT ... FOR UPDATE` on PG；sqlite 依赖事务写锁）
- Create: `apps/web/src/features/review/ReviewPage.tsx`、`ReviewFilters.tsx`（severity 过滤、evidence 跳转）、`apps/web/src/features/revision/ProposalDiff.tsx`（内联 diff + hunk 勾选）、`ProposalActions.tsx`（`A`/`R` 键盘；`guard_status=blocked` 时批准钮禁用 + 原因）
- Tests: `tests/api/test_reviews_revisions.py`、`tests/integration/revision/test_approval_creates_version.py`

**approve 事务契约：**
```python
async def execute(self, *, proposal_id, user_id, idempotency_key, accept_hunks=None):
    async with self.uow_factory() as uow:
        row = await uow.revisions.get_owned_for_update(proposal_id, user_id)   # FOR UPDATE
        if row is None: raise LookupError("proposal not found")                # → 404
        if row.status in ("APPROVED", "VERSION_CREATED"):
            return ProposalResult(proposal=row, replayed=True)                 # 幂等重放
        if row.status != "PROPOSED":
            raise ConflictError("proposal is not approvable")                  # → 409
        active_id, active_hash = await uow.chapters.active_version(row.chapter_id)
        if active_id != row.base_version_id or active_hash != row.before_hash:
            raise ConflictError(code="REVISION_BASE_CONFLICT",
                                details={"current_version_id": active_id})     # → 409 蓝图错误格式
        new_text = apply_hunks(row.after_text, row.hunks_json, accept_hunks)   # 部分接受
        version = await uow.chapters.append_version(row.chapter_id, new_text)
        await uow.chapters.set_active_version(row.chapter_id, version.id)
        row.status = "VERSION_CREATED"; row.idempotency_key = idempotency_key
        await uow.commit()                                                     # 单事务
        # 提交后：emit chapter.version.created 事件 + usage 关联（project/conversation/workflow）
```
并发安全双保险：行锁序列化 + 唯一 `(chapter_id, content_hash)`（append_version 已有去重）。重复 `Idempotency-Key` + 已决状态 → 返回首次结果。

**红线测试（blueprint）：** AI 不能写 active 版本（V2 表面无直写路由；`selection_action` 只产 proposal）；approve 幂等（双调用仅一个 version）；stale base → 409 + fresh diff；部分 hunk 接受保留被拒文本；usage 关联 project/conversation/workflow；跨用户 404；守卫 blocking → `guard_status=blocked` → 批准被拒 422。review 创建持久化 `review_reports`（findings/severity/evidence/scores/快照/usage_call_id）。

**Steps:**
- [ ] 1. 写 0015 迁移 + 改 0016 down_revision；写失败测试（api + integration + vitest）。
- [ ] 2. 实现 domain 状态机 + UoW 用例 + 路由 + reviews 端点。
- [ ] 3. 实现 ReviewPage/ProposalDiff/ProposalActions 并接编辑器批准流（V2-006 的 `A`/`R`）。
- [ ] 4. `git diff --check`；Commit: `feat(quality): add review and approval workflow`

**B3 批次验证：**
```bash
podman compose -f compose.yaml -f compose.test.yaml up -d --build --parallel 1 postgres redis
podman compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest -q tests/api/test_reviews_revisions.py tests/integration/revision tests/migration
podman compose -f compose.yaml -f compose.test.yaml run --rm api-test pytest -q tests/api
podman compose -f compose.yaml -f compose.test.yaml run --rm api-test ruff check proseforge tests
podman compose -f compose.yaml -f compose.test.yaml run --rm web-test
podman compose -f compose.yaml -f compose.test.yaml down -v
```
写 `artifacts/BATCH_VALIDATION_V2_B3.md`。

### Task V2-008: Workflow Studio 与恢复 UX

**Blueprint ref:** `BLUEPRINT/06_WORKFLOW_STUDIO.md`（定义/API-SSE/画布规格）、`DESIGN_SYSTEM_INK.md` §4.3。

**Files:**
- Migration Create: `proseforge/infrastructure/database/migrations/versions/0013_workflow_definitions.py`：
  - `workflow_definitions`：`id, project_id(idx), name, revision, definition_json, created_at, updated_at`，unique(project_id, name, revision)
  - `workflow_node_states`：`id, run_id(idx), node_key, status, checkpoint_json, lease_owner, lease_expires_at, retry_count, reserved_tokens, used_tokens, reserved_cost, used_cost, updated_at`，unique(run_id, node_key)
- Create: `proseforge/application/workflows/definition_service.py`（CRUD + 校验：六种节点类型 intake/plan/write/review/rewrite/export、显式边、回环拒绝）、`proseforge/application/workflows/run_service.py`（从 definition revision 起 run；节点调应用用例不直调 provider；预算=节点前 reserve、完成后记实际、释放未用；超额 → BUDGET_BLOCKED 暂停且不提交半成品版本）、`proseforge/application/workflows/recover_run.py`（租约过期 → RECOVERING → 重排）、`proseforge/api/routes/workflow_definitions.py`、`proseforge/api/routes/workflow_runs.py`（/api/v2，蓝图路由表；控制动作吃 `Idempotency-Key` 头，重复键返回首次结果）
- Modify: `proseforge/api/routes/workflows.py`（v1 兼容保留，事件端点接 live tail）、`proseforge/application/workflows/control.py`（幂等键）
- Frontend Deps: `@xyflow/react`（lockfile 同提交）
- Create: `apps/web/src/features/workflows/WorkflowStudioPage.tsx`、`WorkflowCanvas.tsx`（React Flow：`--paper` 底 + 24px 淡墨点阵；节点竖排小卡 `--font-seal` + ≤30 字摘要 + StatusStamp；1.5px `--ink-mid` 贝塞尔边；拖入新增、拖线建边（回环拒绝提示）、点选右侧抽屉编辑参数、Delete 级联删边二次确认；运行态只读叠加状态章）、`WorkflowRunTimeline.tsx`（事件流 BrushDivider 分隔，点事件定位节点）、`WorkflowBudgetPanel.tsx`（墨池圆形墨碟，墨面高度=预算用量）、`workflowQueries.ts`
- Tests: `tests/api/test_workflow_definitions.py`、`tests/integration/workflows/test_refresh_recovery.py`

**SSE（复用 V2-002 live tail）：** `/api/v2/workflow-runs/{id}/events` = replay（Last-Event-ID）→ live tail 至 terminal/disconnect；15s 心跳；terminal 事件先持久化后广播；刷新 = 先载 snapshot（run + node states）再从游标续播，画布状态与事件游标一致（不允许假"全部完成"章）。

**测试：** pause/resume/cancel/retry 竞争（并发双请求恰一个生效，重复幂等键同结果）；heartbeat/replay；刷新恢复（snapshot + 续播，无重复事件）；预算 blocking（reserve 超额 → BUDGET_BLOCKED，无半成品 version）；checkpoint 重试（retry_count 递增、从失败节点续跑）；terminal 顺序（最后事件必为 terminal 且持久化）。前端：拖拽建边、回环拒绝、运行态印章切换、墨池高度与预算数值一致。

**Steps:**
- [ ] 1. 写 0013 迁移 + 失败测试。
- [ ] 2. 实现 definition/run/recover 服务 + v2 路由 + 控制幂等。
- [ ] 3. 加 `@xyflow/react` 重建 lockfile；实现 Studio 四组件。
- [ ] 4. `git diff --check`；Commit: `feat(workflow): add resumable visual workflow studio`

### Task V2-009: 导出、PWA、无障碍与国际化

**Blueprint ref:** `BLUEPRINT/07_EXPORT_PWA_A11Y_I18N.md`、`DESIGN_SYSTEM_INK.md` §2.1（碑拓主题）§六（对比度数值）。

**Files:**
- Migration Create: `proseforge/infrastructure/database/migrations/versions/0014_export_manifests.py`：`export_manifests(id, project_id(idx), user_id, format, template, title, locale, version_ids_json, content_hashes_json, file_sha256, byte_size, created_at)`
- Modify: `proseforge/application/writing/export_service.py` + `proseforge/api/routes/exports.py`：请求 `{project_id, chapter_range?, version_ids, locale, title, author, template}`；`version_ids` 为空时解析 active 版本为具体 id 快照（不再"裸 current"）；落库 manifest 行 + `file_sha256`；模板三预设 `web-serial（网文连载）|submission（出版投稿）|archive（自存备份）`；DOCX/EPUB 写入 title/author/locale 元数据
- PWA Modify: `apps/web/index.html`（link manifest + theme-color）、`apps/web/public/sw.js`（只缓存静态资产；显式 bypass `/api/`；版本化 cache name）、`apps/web/public/manifest.webmanifest`（补 icons）、`apps/web/src/lib/pwa/register.ts`（离线只读壳提示，不谎称生成成功）
- Deps: `i18next`、`react-i18next`（lockfile 同提交）
- i18n Rewrite: `apps/web/src/lib/i18n.tsx`（i18next，namespaces：`chat/editor/workflows/common`，en+zh 全量 key）、Create `apps/web/src/lib/i18n.test.ts`（key 结构 parity + 日期/数字 locale 格式化）
- a11y Modify: `tokens.css`（`[data-theme="rubbing"]` 碑拓主题全套变量）、设置页主题切换（localStorage 持久化）、全局焦点环 `--ink-mid` 2px、对话框焦点圈定/返回/Esc、`prefers-reduced-motion` 动效归 0、StatusStamp 文字+形状双编码复核
- Modify: `apps/web/e2e/visual-a11y.spec.ts`（axe 扫四页：聊天外壳/编辑器/Workflow Studio/导出对话框，0 critical 0 serious）
- Create: `apps/web/src/features/export/ExportDialog.tsx`（真：格式/章节范围/版本选择/模板预设/哈希展示）、`exportTypes.ts`、`ExportDialog.test.tsx`

**测试：** ExportDialog 渲染与提交参数；i18n parity；碑拓切换后 token 生效（计算样式断言）；导出落库 manifest 且 hash 可复算（下载字节 sha256 == manifest.file_sha256）；SW 不缓存 `/api/`（源码静态断言 + e2e）；axe 0 critical/serious。

**Steps:**
- [ ] 1. 写 0014 迁移 + 失败测试。
- [ ] 2. 实现导出 manifest 持久化 + 模板预设 + 元数据。
- [ ] 3. 实现 PWA 补强 + i18next 迁移 + 碑拓主题 + a11y 修复清单。
- [ ] 4. `git diff --check`；Commit: `feat(web): add export pwa and accessibility surfaces`

**B4 批次验证：**
```bash
podman compose -f compose.yaml -f compose.test.yaml up -d --build --parallel 1 postgres redis
podman compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest -q tests/api/test_workflow_definitions.py tests/integration/workflows tests/api/test_sse_reconnect.py
podman compose -f compose.yaml -f compose.test.yaml run --rm migration-test
podman compose -f compose.yaml -f compose.test.yaml run --rm api-test ruff check proseforge tests
podman compose -f compose.yaml -f compose.test.yaml run --rm web-test
podman compose -f compose.yaml -f compose.test.yaml up -d --wait postgres redis api worker web provider-mock
podman compose -f compose.yaml -f compose.test.yaml run --rm e2e pnpm e2e --grep "professional|a11y"
podman compose -f compose.yaml -f compose.test.yaml down -v
```
写 `artifacts/BATCH_VALIDATION_V2_B4.md`。

### Task V2-010: 真实 E2E 与发布门禁（L2）

**Blueprint ref:** `BLUEPRINT/09_TEST_PLAN.md`（10 步流程）、`10_RELEASE_GATE.md`（门禁清单）、`08`（产物）。

**Files:**
- Rewrite: `apps/web/e2e/v2-professional-flow.spec.ts` → 真实 10 步（认证用户 + 真实 API/DB，UI 操作为主、API 断言兜底）：
  1. 建项目 2. 建会话发消息 3. 等待 assistant 落库完成 + usage 记录 4. 编辑早先用户消息 → 新分支且旧分支不变 5. 再生成 + 候选比较 6. 建并 pin Story Bible 条目（断言下一轮生成快照含该条目 id）7. 选文本请求 review/rewrite 8. diff 展示 → 接受选中 hunk → 新版本 9. 起工作流 pause/resume/cancel/retry → 刷新恢复 10. 导出 Markdown/DOCX/EPUB 校验 manifest/hash
- Create: `artifacts/v2-openapi.json`（`create_app().openapi()` 导出脚本 `scripts/dump_openapi.py`）、`artifacts/v2-schema-check.txt`（逐端点核对 ownership/idempotency/pagination/replay/redaction 的人工+静态检查记录）
- Rewrite: `artifacts/V2_FINAL_VALIDATION.md`（真实证据：每条命令、退出码、计数、镜像 digest、`podman version`、trace id、下载哈希；明示未测平台/场景）
- Modify: `artifacts/VALIDATION_STATUS.md`（V2 状态更新）、`CHANGELOG.md`（V2 条目）
- Modify: `docs/plans/`（本计划定稿归档为 `docs/plans/PROSEFORGE_V2_CHAT_WORKSPACE_IMPLEMENTATION_PLAN.md`）

**L2 全矩阵（Podman，逐服务串行）：** legacy-test、api-test（全量）、contract-test、migration-test、recovery-test、web-test、e2e（全量，含恢复的 ordinary-user-smoke）、ruff。故障注入（tests/fault_injection）随 api 矩阵跑。V1.5 门禁不得回退（native 相关测试随全量 pytest 走）。

**Release gate 核对（10_RELEASE_GATE.md 12 条逐条过）：** 三端外壳 / 不可变历史 / 模型目录真实 / Inspector 含 omitted 原因 / Story Bible 结构化 ownership 安全 / 提案-only 写正文 / usage 五级归因 / SSE 刷新续播 + 控制幂等 / 导出版本哈希 / PWA 不缓存凭据与正文 / 中英+键盘+焦点+reduced motion / V1.5 门禁仍绿。

**Steps:**
- [ ] 1. 写 10 步 e2e（先红）。
- [ ] 2. 修通全量 L2；生成 openapi/schema 产物。
- [ ] 3. 写 V2_FINAL_VALIDATION.md + 更新 VALIDATION_STATUS.md。
- [ ] 4. Commit: `test(v2): validate real professional workflows`

## 自查（spec 覆盖）

- V2-001~010 ↔ BLUEPRINT 00 任务表：一一对应；L1 批次划分 ↔ TEST_EXECUTION_POLICY §四。
- 用户排查的 V2 缺口覆盖：聊天上下文（V2-002/005）✓、Tiptap/React Flow/真路由（V2-001/006/008）✓、迁移 0013/0014/0015（V2-008/009/007）✓、approve 并发（V2-007）✓、真实 E2E（V2-001 解除 skip + V2-010）✓、缺失测试文件（各任务 Tests 段）✓、v2-openapi/schema 产物（V2-010）✓。
- 编号说明：0015 内容为 review_reports + 提案加固列（blueprint 0015 原为"model/reasoning snapshots"——消息快照列已在 0010，按 blueprint 条件从缺；review_reports 是 V2-007 蓝图明确要求的持久化结构，且 0012 实际只建了 revision_proposals）。此偏差在迁移 docstring 与批次文档中注明。
- Non-goal（显式）：不重构 v1 `generate_novel` 直写工作流；不触碰 V3 agent 代码与 0016–0024 迁移；不做自由模板编辑器（blueprint 明令）；图片/PDF 附件 defer。

## 执行交接

执行模式二选一（见 ExitPlanMode 选项）：Subagent-Driven（每任务派新子代理 + 两阶段评审，推荐——上下文隔离好、每任务有新鲜评审）或 Inline（本会话按批执行、批次检查点停顿）。两种模式共同遵守：任务级不起容器；L1 批次验证不过不进入下一批；每任务独立提交（信息用 blueprint 任务表原文）；计划定稿先归档到 `docs/plans/PROSEFORGE_V2_CHAT_WORKSPACE_IMPLEMENTATION_PLAN.md`。
