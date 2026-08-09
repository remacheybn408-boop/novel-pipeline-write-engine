# 常见问题

## 页面显示 API Offline

执行 `docker compose ps`，确认 api、web、postgres、redis 均为 `healthy`。查看 `docker compose logs api`。API 启动会自动运行迁移和缺失表修复，首次启动可能需要几秒。

## 第一次登录失败

确认使用的是首次 setup 创建的账户。setup 只允许创建第一个 owner；如果已有账户，直接登录，不要重复 setup。

## 容器重启后数据还在吗

只要没有使用 `down -v`，PostgreSQL、Redis、Blob 和备份都保存在 Docker volumes 中。可以用 `docker compose down` 后再 `up -d` 验证持久化。

## 测试

所有测试命令见 [DOCKER_TESTING.md](DOCKER_TESTING.md)，不要在宿主机直接执行 pytest、npm 或 pnpm。
