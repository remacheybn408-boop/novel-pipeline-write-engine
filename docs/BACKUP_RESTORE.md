# 备份与恢复

备份包含 Blob 文件、清单、SHA-256 校验值、应用版本和迁移版本；如果提供数据库 dump，也会一并打包。

```bash
docker compose run --rm api proseforge backup create --source /data --root /data/backups --include-database
docker compose run --rm api proseforge backup list --root /data/backups
docker compose run --rm api proseforge backup verify /data/backups/proseforge-<timestamp>.tar.gz
docker compose run --rm api proseforge backup restore /data/backups/proseforge-<timestamp>.tar.gz --destination /data/restore-staging
docker compose run --rm api proseforge backup restore /data/backups/proseforge-<timestamp>.tar.gz --destination /data/restore-staging --restore-database-url postgresql://proseforge:proseforge@postgres:5432/proseforge_staging
```

恢复始终先到 staging 目录；校验失败、路径穿越或缺少清单条目时会拒绝恢复。恢复前保留当前实例的独立备份，不直接覆盖唯一在线数据库。
