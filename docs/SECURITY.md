# 安全边界

- 用户、项目、对话、文件、凭据和导出都按 owner 做服务端授权。
- 凭据使用 AES-GCM 加密，关联用户、提供商和记录 ID 的 associated data。
- 会话使用 HttpOnly cookie；前端不把原始 token 写入 localStorage。
- 上传文件限制扩展名、大小、Zip 路径和压缩炸弹风险。
- 生产环境拒绝占位密钥和相对存储路径。
- CI 包含 Ruff、依赖审计、Trivy、Gitleaks 和 SBOM 产物。

发现安全问题请不要公开提交密钥或可利用样例，先通过私下渠道报告。
