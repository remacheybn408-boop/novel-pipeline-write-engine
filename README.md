# ProseForge

ProseForge 是一个本地部署的长篇小说 AI 写作工作台。你的文稿、设定和对话都存在自己的机器上,模型走你自己配置的接口(OpenAI 兼容),AI 只做参谋——**所有修改建议都必须经你批准才会写进正文**。

## 它能帮你做什么

- **写作工作室**:专注的章节编辑器,选中文段即可让 AI 审校或改写;每次采纳生成一个不可变版本,历史永远可回溯、可对比
- **AI 陪聊(Companion Chat)**:围绕当前作品对话,分支、重生成、候选对比一应俱全;AI 回答会自动带上你的设定上下文
- **Work 普通模式**:单个模型驱动的写作助手,聊天框里直接派发写章/审校/改写意图,支持附件(.txt/.md/.pdf/.docx/.xlsx 等)上传与粘贴
- **Work 集群模式(Agent Swarm)**:六席位人格化 Agent 协作的全自动写作流水线——
  - **马歇尔 · 总调度**:意图路由与批次派发,模型访问不稳定时自动暂停并在聊天框提醒你
  - **福尔摩斯 · 分析**:结构/人物/伏笔三专项并行分析,融合成逐章工作流
  - **莎士比亚 · 写作**:四路并行场景草稿(含去 AI 味的「人味写作」专席),融合择优成稿;必读上一章全文才动笔,跨章连贯
  - **约翰逊 · 审校**:连续性/对抗性/文风三评审互不可见 + 合议主持裁定,字数不足、伏笔未收、AI 腔一律打回
  - **米开朗基罗 · 改写**:按审校指令定点改写,改后自动复核证据引用与接缝
  - **奥莉维亚 · 动态承诺**:章节承诺/钩子/伏笔台账——写前出契约卡、写后逐条核对兑现、图尾登记新承诺,已完成打勾绝不重复生效
- **长书不失忆**:章节摘要金字塔(L0 段级/L1 章级,修订自动失效重算) + 承诺台账 + 叙事 RAG(本地 BGE-M3 向量检索,原文 canon 优先于衍生梗概);每个席位独立上下文预算(真实窗口的 65%),将尽时该席位自动瘦身上下文,绝不波及邻座
- **故事圣经(Story Bible)**:结构化的设定库(人物、世界观、时间线),钉选的事实会注入后续每一次生成,防止 AI 写串设定
- **模型元数据注册表**:内置经调研核实的真实上下文窗口/max_output/思考强度档位(含 OpenAI 兼容网关与中转站跨厂商解析),面板显示真实窗口而非拍脑袋值
- **弹性思考强度**:集群默认总调度/分析=max、其余席位=high;小 JSON 任务自动降档保护结构化输出,正文任务自动上调输出预算防思考吃正文
- **上下文透视(Context Inspector)**:每次生成前能看到 AI 到底读到了什么、为什么这些条目被选中、花了多少 token
- **用量统计**:每个项目、每场对话、每次工作流的 token 花费可查;插件页另有按模型分组的用量面板(调用数/各类 token/成本/延迟,24 小时/7 天/30 天)
- **导出**:一键导出 Markdown / DOCX / EPUB,每份文件附带来源版本号和内容哈希,可校验未被篡改;批量写作支持按章下载与一键打包 ZIP

## 快速开始(Docker)

只需要 Docker Desktop,不需要安装 Python 或 Node。

```bash
git clone <仓库地址>
cd ProseForge
copy .env.example .env        # Windows;Linux/macOS 用 cp
```

编辑 `.env`,把两处占位值换成随机长字符串:`PROSEFORGE_MASTER_KEY`(32 字节 base64)和 `PROSEFORGE_JWT_SECRET`。生产环境会拒绝默认占位值。

```bash
docker compose up --build -d --wait
```

打开 <http://localhost:3000>:

1. **创建账号**——首次打开进入设置页,这个实例只有一个账号(数据全在你本地,账号只防误触)
2. **配置模型**——进 Settings,添加你的模型凭证:任意 OpenAI 兼容接口(base_url + API key),按项目选择模型;删除模型后聊天框的模型选择会同步清理
3. 新建项目,开始写作

API 健康检查:`GET http://localhost:8000/api/v1/health/live` 与 `/ready`。

## 原生安装包(免 Docker)

V1.5 起提供原生安装包:打包好的运行时,目标机器什么都不用装。解包后运行 `proseforge web` 即可(默认 <http://127.0.0.1:8000>),数据自动放在系统标准目录(Windows `%LOCALAPPDATA%\ProseForge`,Linux `~/.local/share/ProseForge`)。

- **Windows**:Inno Setup 安装器(安装/升级自动备份/卸载保留文稿)
- **Linux**:deb / rpm 包或免 root tarball,带 `systemctl --user` 服务
- **macOS**:打包脚本就绪,但需要 macOS 机器执行,当前未提供成品

运维命令:`proseforge doctor`(体检)、`proseforge backup create|verify|restore`(备份/校验/恢复)、`proseforge upgrade`(升级:锁定→备份→迁移→健康检查→失败自动回滚)。

自己构建安装包:

```bash
powershell -File scripts/build_native.ps1 -Target windows      # Windows
bash scripts/build_native.sh --target linux --skip-sign        # Linux(容器内构建)
```

## 数据与安全

- 文稿存 PostgreSQL(Docker 卷或本机数据目录),附件走内容寻址 BlobStore
- 模型凭证加密存储,密钥就是 `.env` 里的 `PROSEFORGE_MASTER_KEY`——丢了它凭证无法解密
- 向量模型(BGE-M3)随安装包/镜像离线捆绑,无网环境 RAG 照样可用
- 升级前自动备份;`proseforge backup` 系列命令可随时手动备份并校验

## 开发与测试

全部测试在 Docker 内运行,宿主机零依赖:

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test      # 后端全量(1600+)
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test      # 前端 120+ 与构建
docker compose -f compose.yaml -f compose.test.yaml run --rm e2e           # 浏览器端到端
```

另有 contract、migration、recovery、故障注入与安全套件;发布验证台账见 `artifacts/`(V1.5/V2/V3 均已 PASS,macOS 安装器除外)。

## 目录

```text
proseforge/    后端(API、领域服务、集群执行器)
apps/web/      前端(React)
packs/         人格文件与技能包(六席位人格 + 写作技法参考)
docker/        镜像与编排
docs/          设计文档与运维手册
artifacts/     发布验证证据
packaging/     原生安装包构建
```

详细说明:[docs/DOCKER_TESTING.md](docs/DOCKER_TESTING.md)、[docs/INSTALL.md](docs/INSTALL.md)、[docs/web_v1_architecture.md](docs/web_v1_architecture.md)。

## License

AGPL-3.0
