# ProseForge Web 安装

ProseForge 的开发、测试和运行都通过 Docker 完成，宿主机不需要安装 Python 或 Node.js。

```powershell
docker compose build api worker scheduler web
docker compose up -d
docker compose ps
```

打开 `http://localhost:3000`。第一次使用选择 “First run”，创建唯一的 owner 账户；之后使用该账户登录。不要使用 `docker compose down -v`，否则会删除数据库卷。

升级代码后重新执行 `docker compose up -d --build`。API 启动时会先执行迁移，并检查缺失表，完成后才接受请求。
