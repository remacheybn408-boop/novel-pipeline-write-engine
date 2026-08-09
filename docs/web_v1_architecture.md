# Web v1 架构

浏览器通过 Nginx 访问 React SPA 和 `/api` 反向代理。FastAPI 负责认证、所有权、项目、章节、上下文、大纲、对话和工作流；PostgreSQL 是持久化事实来源，Redis/Celery 承载后台任务，BlobStore 保存附件和导出物。

旧版 `src` 代码只通过 legacy engine/import 边界使用。新 Web 功能依赖 `proseforge` 的 domain、application、infrastructure 分层，SSE 事件写入 PostgreSQL 后才能重放，避免浏览器刷新丢失增量。
