# Legacy SQLite migration

旧版 SQLite 工作区只通过兼容 CLI 导入，Web 运行时不会继续写入旧 `workspace/` 目录。

## 导入前

1. 保留原 SQLite 文件的只读副本。
2. 在 `.env` 中设置 PostgreSQL、BlobStore 和 JWT 配置。
3. 先启动数据库和 Redis：

```bash
docker compose up -d postgres redis
```

## 执行导入

导入任务会扫描、映射、写入新项目，并在归档目录保留扫描结果；失败时不会删除源 SQLite：

```bash
docker compose run --rm api \
  proseforge migrate legacy \
  --workspace /data/legacy-workspace \
  --archive-root /data/backups/legacy-import
```

指定 Web 用户作为项目所有者时，使用 `--owner-id`。导入完成后，通过 Web 项目列表检查章节数量、标题和版本摘要，再开始生成任务。

## 安全边界

- 迁移失败不会删除或覆盖源 SQLite。
- 新数据进入 PostgreSQL 和 BlobStore；Redis 只负责队列、锁和缓存。
- 迁移报告中的校验失败必须先处理，不能把未验证内容直接设为正式版本。
- 外部 Codex、Hermes、Claude Code 插件不是 Web v1 的运行入口。
