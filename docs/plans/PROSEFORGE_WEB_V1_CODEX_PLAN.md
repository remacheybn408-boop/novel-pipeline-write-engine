# ProseForge Web v1.0 Codex Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 ProseForge 从“本地 Docker 小说内核 + Codex/Hermes/Claude Code 外挂 Agent”改造成一个暖色、GPT 式、自托管、数据持久化、支持国内外模型原生 API、能够自动完成大纲解析、规划、写作、审稿、改写、自检和导出的 Web 产品。

**Architecture:** 保留当前已经稳定的小说工程内核和 Guard，通过唯一的 `LegacyNovelEngineAdapter` 接入新的 Application 层；新 Web 路径采用 React SPA + FastAPI + PostgreSQL + Celery + Redis + BlobStore。所有模型调用进入独立 Provider Adapter，所有长工作流使用数据库 checkpoint 和幂等键，任何 Route、前端组件和 Provider 都不能直接调用旧 `src/pipeline`。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL 16、pgvector、Redis 7、Celery 5、httpx、cryptography、zstandard、React 19、TypeScript、Vite、TanStack Router/Query、Zustand、Tailwind CSS、shadcn/ui、Tiptap、Dexie、Vitest、Playwright、Docker Compose。

## Global Constraints

- 基线仓库：`remacheybn408-boop/ProseForge`
- 基线分支：`master`
- 基线提交：`d976bcb52ddcadfb3092e8702b4dea534fb73ca4`
- 所有开发必须在新 worktree 和 `feat/web-v1` 分支完成，不直接修改 `master`。
- 先保存并通过当前 Docker 测试基线，再开始删除或迁移代码。
- 不按工作日拆分；按任务和验收门连续执行。
- 外挂 Codex、Hermes、Claude Code 插件全部删除。
- Anthropic Claude API 和 OpenAI API 仍必须作为原生模型 Provider 保留。
- 新代码只能 import `proseforge.*`；只有 `proseforge/infrastructure/legacy_engine/` 可以 import 旧 `src.*`。
- API Route 禁止直接访问 SQLAlchemy Session、Provider、Celery 和真实文件路径。
- Domain 禁止 import FastAPI、SQLAlchemy、Celery、Redis、httpx、argparse。
- 浏览器禁止直接请求模型厂商，禁止保存完整 API Key。
- API Key 使用 AES-256-GCM 加密；主密钥通过 Docker Secret 或受限环境变量注入。
- PostgreSQL 是业务主数据；Redis 只用于队列、锁、缓存和 PubSub。
- 流式消息必须先落库，再发起模型请求；每个 chunk 可恢复。
- 工作流状态、步骤、checkpoint 和事件必须持久化到 PostgreSQL。
- 正文必须 staging + version，不直接覆盖 canonical 版本。
- 同一正文 hash 不得生成重复正式版本。
- 语义摘要不能宣传为数学意义无损；原始消息和原始章节永不因压缩被删除。
- 模型目录动态同步；不能用永久硬编码列表表示“支持所有最新模型”。
- 不以 LangChain、LiteLLM、OpenRouter 作为核心 Provider 层。
- 所有阶段在 Docker 中测试。
- 每项任务先写失败测试、确认失败、最小实现、确认通过、提交。
- 一个任务一个可审查提交；不得把多个独立子系统塞进一个提交。
- 遇到测试失败必须修复，不得删除或跳过测试来取得绿色结果。
- 完成前必须执行故障注入、备份恢复和 `compose down/up` 数据持久化测试。

---

# 1. Codex 执行契约

把以下规则写入仓库根目录 `AGENTS.md`，Codex 每次开始任务前先读取：

```markdown
# ProseForge Web v1 Codex Rules

1. Read `docs/plans/PROSEFORGE_WEB_V1_CODEX_PLAN.md` before changing code.
2. Execute tasks in numeric order.
3. Work only on branch `feat/web-v1`.
4. Use test-driven development: failing test, minimal implementation, passing test.
5. New application code imports only `proseforge.*`.
6. Only `proseforge/infrastructure/legacy_engine/` may import `src.*`.
7. API routes call application use cases; routes never query the database directly.
8. Provider adapters never import application services or repositories.
9. Redis is disposable; PostgreSQL and BlobStore are durable.
10. Do not delete legacy data during migration.
11. Do not hardcode a permanent model list.
12. Do not expose API keys, prompts, or full novel text in logs.
13. Do not claim completion until Docker unit, integration, E2E, recovery, and backup tests pass.
14. Commit after each completed task using the commit message specified in the plan.
15. Continue through the plan without asking for routine confirmation; stop only when a requirement is genuinely impossible or unsafe.
```

## 1.1 每个任务的固定动作

每个任务必须执行：

```bash
git status --short
docker compose -f compose.yaml -f compose.test.yaml run --rm <test-service> <test-command>
git diff --check
git status --short
git add <exact-files>
git commit -m "<specified-message>"
```

## 1.2 阶段验收门

任何阶段结束时执行：

```bash
docker compose -f compose.yaml -f compose.test.yaml build
docker compose -f compose.yaml -f compose.test.yaml run --rm legacy-test
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test
```

只有全部退出码为 0 才进入下一阶段。

---

# 2. 边界设计

## 2.1 最终依赖方向

```text
apps/web
   ↓ HTTP/SSE
proseforge/api
   ↓ UseCase
proseforge/application
   ↓ Ports
proseforge/domain
   ↑ implementations
proseforge/infrastructure
   ├── database
   ├── providers
   ├── blob
   ├── queue
   ├── legacy_engine
   └── observability
```

依赖只能向内：

```text
api -> application -> domain
infrastructure -> domain/application ports
web -> api contract
```

禁止：

```text
domain -> infrastructure
application -> FastAPI
api -> SQLAlchemy model
web -> provider vendor
provider -> repository
route -> src.pipeline
workflow task -> raw filesystem path
```

## 2.2 旧内核唯一入口

新建：

```text
proseforge/domain/ports/novel_engine.py
proseforge/infrastructure/legacy_engine/adapter.py
```

接口：

```python
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class PreparedChapterContext:
    project_id: str
    chapter_id: str
    chapter_no: int
    chapter_type: str
    context_text: str
    context_metadata: dict[str, object]
    context_hash: str

@dataclass(frozen=True)
class RuleQualityResult:
    status: str
    can_commit: bool
    blocked_by: tuple[str, ...]
    warnings: tuple[dict[str, object], ...]
    artifacts: tuple[str, ...]

@dataclass(frozen=True)
class CommitChapterResult:
    version_no: int
    content_hash: str
    word_count: int
    artifacts: tuple[str, ...]

class NovelEnginePort(Protocol):
    def prepare_chapter(
        self,
        *,
        legacy_slot: str,
        novel_slug: str,
        novel_title: str,
        volume_no: int,
        chapter_no: int,
        chapter_type: str,
    ) -> PreparedChapterContext: ...

    def run_rule_quality(
        self,
        *,
        legacy_slot: str,
        novel_slug: str,
        novel_title: str,
        volume_no: int,
        chapter_no: int,
        chapter_type: str,
        staged_file: str,
    ) -> RuleQualityResult: ...

    def commit_chapter(
        self,
        *,
        legacy_slot: str,
        novel_slug: str,
        novel_title: str,
        volume_no: int,
        chapter_no: int,
        chapter_type: str,
        staged_file: str,
    ) -> CommitChapterResult: ...
```

只有 Adapter 可以调用：

```text
src.application.pipeline_service
src.pipeline.pre
src.pipeline.post
src.pipeline.ingest
src.pipeline.rewrite
src.pipeline.revision_diff_report
```

## 2.3 现有文件的具体处理

| 当前路径 | 处理 | 目标 |
|---|---|---|
| `plugin/proseforge-codex/` | 删除 | 不再作为产品运行入口 |
| `plugin/proseforge-Hermes/` | 删除 | 不再作为产品运行入口 |
| `plugin/proseforge-claude/` | 删除 | 不再作为产品运行入口 |
| `.claude/` | 删除 | 删除 Claude Code 项目配置 |
| `install_plugin.py` | 删除 | 无插件安装 |
| `install.sh` | 改写 | 只负责 Docker Web 启动提示 |
| `src/application/pipeline_service.py` | 保留兼容 | 旧 CLI 使用；新 Web 不直接调用 |
| `src/interfaces/cli.py` | 保留兼容并扩展 | doctor、legacy migrate、backup |
| `src/runtime.py` | 保留兼容 | 新 Web 项目身份不使用 active slot |
| `src/pipeline/pre.py` | 保留并由 Adapter 包装 | 写前上下文 |
| `src/pipeline/post.py` | 拆除外挂审稿调用 | 只做确定性质量和入库 |
| `src/pipeline/ingest.py` | 保留并修复幂等 | Adapter commit |
| `src/agents/` | 迁移为 analyzers | 明确是规则分析器 |
| `src/guards/` | 通过 quality adapter 复用 | 规则自检 |
| `src/rag/` | 迁移到 context engine infra | 检索能力 |
| `compose.yaml` | 重写 | web/api/worker/scheduler/postgres/redis |
| `Dockerfile` | 拆分 | `docker/api.Dockerfile` 等 |
| `pyproject.toml` | 重写依赖分组 | api/worker/dev/legacy |
| `README.md` | Web-first | Docker Compose 安装 |
| `config.example.json` | 仅 legacy | 新配置使用环境变量和数据库 |
| `workspace/` | 只读迁移源 | Web 运行时不继续写入 |
| `database/schema.sql` | legacy 归档 | PostgreSQL 使用 Alembic |

## 2.4 新 Web 运行路径

```text
HTTP request
  -> API route
  -> Application UseCase
  -> UnitOfWork
  -> Repository
  -> Workflow command
  -> Celery task
  -> ContextCompiler
  -> ModelProvider
  -> QualityService
  -> NovelEnginePort
  -> PostgreSQL/BlobStore
  -> EventLog
  -> SSE
```

---

# 3. 目标文件地图

## 3.1 根目录

```text
AGENTS.md
compose.yaml
compose.test.yaml
.env.example
alembic.ini
pyproject.toml
README.md
docs/plans/PROSEFORGE_WEB_V1_CODEX_PLAN.md
```

## 3.2 Python

```text
proseforge/
  __init__.py
  settings.py
  cli/main.py
  api/main.py
  api/dependencies.py
  api/errors.py
  api/routes/
  api/sse/
  application/
  domain/
  infrastructure/
  providers/
  context_engine/
  workflows/
  prompts/
```

## 3.3 Web

```text
apps/web/
  package.json
  pnpm-lock.yaml
  vite.config.ts
  tsconfig.json
  src/
  tests/
  e2e/
```

## 3.4 Docker

```text
docker/api.Dockerfile
docker/worker.Dockerfile
docker/web.Dockerfile
docker/nginx.conf
docker/entrypoint-api.sh
docker/entrypoint-worker.sh
```

---

# Phase 0 — Freeze and Guard the Baseline

### Task 0: Create the isolated branch, plan location, and Codex rules

**Files:**
- Create: `AGENTS.md`
- Create: `docs/plans/PROSEFORGE_WEB_V1_CODEX_PLAN.md`
- Create: `docs/baseline/current-revision.txt`
- Create: `docs/baseline/current-tests.md`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: current repository at commit `d976bcb52ddcadfb3092e8702b4dea534fb73ca4`
- Produces: immutable baseline record and Codex execution rules

- [ ] **Step 1: Create worktree and branch**

Run:

```bash
git fetch origin master
git worktree add ../ProseForge-web-v1 -b feat/web-v1 origin/master
cd ../ProseForge-web-v1
git rev-parse HEAD
```

Expected:

```text
d976bcb52ddcadfb3092e8702b4dea534fb73ca4
```

- [ ] **Step 2: Write `AGENTS.md`**

Use the exact content from section 1 of this plan.

- [ ] **Step 3: Record revision**

Write:

```text
repository=remacheybn408-boop/ProseForge
branch=master
revision=d976bcb52ddcadfb3092e8702b4dea534fb73ca4
```

to `docs/baseline/current-revision.txt`.

- [ ] **Step 4: Run legacy Docker tests**

Run:

```bash
docker compose -f compose.yaml build proseforge
docker compose -f compose.yaml run --rm test
```

Expected:

```text
413 passed
```

If the exact count changes because the baseline branch has advanced, record the actual count and exact SHA before changing code.

- [ ] **Step 5: Save baseline report**

`docs/baseline/current-tests.md` must include:

```markdown
# Current Test Baseline

- Revision: `d976bcb52ddcadfb3092e8702b4dea534fb73ca4`
- Command: `docker compose -f compose.yaml run --rm test`
- Expected result: `413 passed`
- JUnit artifact: `artifacts/pytest.xml`
```

- [ ] **Step 6: Update `.gitignore`**

Ensure it contains:

```gitignore
.env
.env.*
!.env.example
data/
backups/
artifacts/
apps/web/node_modules/
apps/web/dist/
playwright-report/
test-results/
.coverage
htmlcov/
```

- [ ] **Step 7: Commit**

```bash
git add AGENTS.md docs/baseline .gitignore docs/plans/PROSEFORGE_WEB_V1_CODEX_PLAN.md
git commit -m "docs: freeze web v1 implementation baseline"
```

---

### Task 1: Add architecture import-boundary tests before moving code

**Files:**
- Create: `tests/architecture/test_import_boundaries.py`
- Create: `tests/architecture/test_forbidden_paths.py`

**Interfaces:**
- Consumes: repository source tree
- Produces: executable dependency-boundary enforcement

- [ ] **Step 1: Write failing boundary test**

```python
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_DOMAIN_PREFIXES = {
    "fastapi",
    "sqlalchemy",
    "celery",
    "redis",
    "httpx",
    "argparse",
}

def imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names

def test_domain_does_not_import_infrastructure_frameworks() -> None:
    domain = ROOT / "proseforge" / "domain"
    assert domain.exists(), "proseforge/domain must be created"
    violations: list[str] = []
    for path in domain.rglob("*.py"):
        for name in imports_in(path):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_DOMAIN_PREFIXES):
                violations.append(f"{path.relative_to(ROOT)} imports {name}")
    assert violations == []
```

- [ ] **Step 2: Write the old-core isolation test**

```python
def test_only_legacy_engine_may_import_src() -> None:
    package = ROOT / "proseforge"
    assert package.exists(), "proseforge package must be created"
    violations: list[str] = []
    for path in package.rglob("*.py"):
        relative = path.relative_to(package)
        if relative.parts[:2] == ("infrastructure", "legacy_engine"):
            continue
        for name in imports_in(path):
            if name == "src" or name.startswith("src."):
                violations.append(f"{path.relative_to(ROOT)} imports {name}")
    assert violations == []
```

- [ ] **Step 3: Run and verify failure**

```bash
docker compose -f compose.yaml run --rm test -m pytest tests/architecture -q
```

Expected failure:

```text
proseforge/domain must be created
```

- [ ] **Step 4: Create only package placeholders needed for a meaningful boundary**

Create:

```text
proseforge/__init__.py
proseforge/domain/__init__.py
proseforge/infrastructure/__init__.py
proseforge/infrastructure/legacy_engine/__init__.py
```

Each file contains a one-line module docstring.

- [ ] **Step 5: Run and verify pass**

```bash
docker compose -f compose.yaml run --rm test -m pytest tests/architecture -q
```

Expected:

```text
2 passed
```

- [ ] **Step 6: Commit**

```bash
git add proseforge tests/architecture
git commit -m "test: enforce web v1 architecture boundaries"
```

---

# Phase 1 — Remove External Agent Surfaces Without Breaking the Core

### Task 2: Remove Codex, Hermes, and Claude Code plugin surfaces

**Files:**
- Delete: `plugin/proseforge-codex/`
- Delete: `plugin/proseforge-Hermes/`
- Delete: `plugin/proseforge-claude/`
- Delete: `.claude/`
- Delete: `install_plugin.py`
- Modify: `install.sh`
- Modify: `README.md`
- Modify: tests that explicitly execute plugin wrappers

**Interfaces:**
- Consumes: formal CLI `src.interfaces.cli`
- Produces: repository with no external-agent runtime dependency

- [ ] **Step 1: Write failing repository-surface test**

Create `tests/architecture/test_external_agent_surfaces_removed.py`:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def test_external_agent_surfaces_are_removed() -> None:
    forbidden = [
        ROOT / "plugin" / "proseforge-codex",
        ROOT / "plugin" / "proseforge-Hermes",
        ROOT / "plugin" / "proseforge-claude",
        ROOT / ".claude",
        ROOT / "install_plugin.py",
    ]
    assert [str(path.relative_to(ROOT)) for path in forbidden if path.exists()] == []
```

- [ ] **Step 2: Run and verify failure**

```bash
docker compose -f compose.yaml run --rm test -m pytest \
  tests/architecture/test_external_agent_surfaces_removed.py -q
```

Expected: failure listing existing plugin paths.

- [ ] **Step 3: Delete the paths**

```bash
rm -rf plugin/proseforge-codex
rm -rf plugin/proseforge-Hermes
rm -rf plugin/proseforge-claude
rm -rf .claude
rm -f install_plugin.py
```

Do not delete `plugin/` if unrelated content remains.

- [ ] **Step 4: Replace `install.sh`**

Use:

```bash
#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required." >&2
  exit 1
fi

test -f .env || cp .env.example .env
docker compose -f compose.yaml up -d --build
echo "ProseForge Web is starting. Open http://localhost:3000"
```

Then:

```bash
chmod +x install.sh
```

- [ ] **Step 5: Remove plugin language from README**

Replace plugin installation sections with a temporary migration notice:

```markdown
## Web v1 migration

External Codex, Hermes, and Claude Code plugin surfaces have been removed.
The supported entrypoints are the compatibility CLI and the Web API.
Model vendors are connected through native provider adapters.
```

- [ ] **Step 6: Update wrapper-only tests**

Delete only tests whose sole purpose is invoking deleted wrapper scripts.  
For behavior also covered by core tests, keep the core tests.  
For behavior not covered, move the assertion to `tests/legacy/` and call `src.interfaces.cli.main()` directly.

Example:

```python
from src.interfaces.cli import main

def test_legacy_cli_doctor(tmp_path, capsys):
    code = main(["--project-root", str(tmp_path), "doctor"])
    assert code == 0
    assert '"status": "ok"' in capsys.readouterr().out
```

- [ ] **Step 7: Verify no external surfaces remain**

```bash
grep -R "proseforge-codex" . --exclude-dir=.git || true
grep -R "proseforge-Hermes" . --exclude-dir=.git || true
grep -R "proseforge-claude" . --exclude-dir=.git || true
grep -R "Claude Code" . --exclude-dir=.git || true
```

Expected: only historical documentation under `docs/baseline` or this plan may match.

- [ ] **Step 8: Run full legacy suite**

```bash
docker compose -f compose.yaml run --rm test
```

Expected: all retained tests pass.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: remove external agent plugin surfaces"
```

---

### Task 3: Rename deterministic agents to analyzers

**Files:**
- Create: `proseforge/domain/quality/analyzers/`
- Move: `src/agents/base_agent.py`
- Move: `src/agents/prose.py`
- Move: `src/agents/plot.py`
- Move: `src/agents/character.py`
- Move: other deterministic analyzer files under `src/agents/`
- Modify: `src/agents/orchestrator.py`
- Modify: `src/pipeline/post.py`
- Test: `tests/quality/test_analyzer_compatibility.py`

**Interfaces:**
- Consumes: existing deterministic review functions
- Produces: analyzers with no claim of being external agents

- [ ] **Step 1: Write compatibility test**

```python
from proseforge.domain.quality.analyzers.prose_analyzer import ProseAnalyzer

def test_prose_analyzer_returns_structured_result() -> None:
    result = ProseAnalyzer().review("他不禁倒吸一口凉气。", chapter_no=1)
    assert isinstance(result, dict)
    assert "findings" in result
```

- [ ] **Step 2: Verify failure**

```bash
docker compose -f compose.yaml run --rm test -m pytest \
  tests/quality/test_analyzer_compatibility.py -q
```

Expected: import failure.

- [ ] **Step 3: Move code with history**

Use `git mv` for each deterministic module. Rename classes:

```text
BaseAgent -> BaseAnalyzer
ProseAgent -> ProseAnalyzer
PlotAgent -> PlotAnalyzer
CharacterAgent -> CharacterAnalyzer
```

Do not rename external JSON fields in the same commit if legacy tests depend on them.

- [ ] **Step 4: Add compatibility imports**

Temporary `src/agents/prose.py`:

```python
from proseforge.domain.quality.analyzers.prose_analyzer import ProseAnalyzer

ProseAgent = ProseAnalyzer

__all__ = ["ProseAgent", "ProseAnalyzer"]
```

Use the same pattern for the other moved classes.

- [ ] **Step 5: Remove `_post_agent_review` from `src/pipeline/post.py`**

Delete:

```text
_post_agent_review()
run_agent_review()
project_root/reports/agent_reviews
```

Keep deterministic Guard execution. Model review will be implemented in the new workflow.

- [ ] **Step 6: Run focused and full tests**

```bash
docker compose -f compose.yaml run --rm test -m pytest \
  tests/quality/test_analyzer_compatibility.py -q
docker compose -f compose.yaml run --rm test
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: rename deterministic agents as analyzers"
```

---

# Phase 2 — Establish the New Python Package and Stable Ports

### Task 4: Add dependency groups and production package entrypoints

**Files:**
- Modify: `pyproject.toml`
- Create: `proseforge/cli/main.py`
- Create: `proseforge/api/main.py`
- Create: `tests/unit/test_package_entrypoints.py`

**Interfaces:**
- Consumes: Python packaging
- Produces: `proseforge`, `proseforge-api`, `proseforge-worker` install targets

- [ ] **Step 1: Write failing entrypoint tests**

```python
from fastapi import FastAPI

def test_api_app_exists() -> None:
    from proseforge.api.main import app
    assert isinstance(app, FastAPI)

def test_cli_main_is_callable() -> None:
    from proseforge.cli.main import main
    assert callable(main)
```

- [ ] **Step 2: Update `pyproject.toml`**

Use dependency groups:

```toml
[project]
name = "proseforge"
version = "1.0.0.dev0"
requires-python = ">=3.12"
dependencies = [
  "pydantic>=2.11,<3",
  "pydantic-settings>=2.9,<3",
  "orjson>=3.10,<4",
  "structlog>=25.1,<26",
  "zstandard>=0.23,<1",
]

[project.optional-dependencies]
api = [
  "fastapi>=0.116,<1",
  "uvicorn[standard]>=0.35,<1",
  "sqlalchemy[asyncio]>=2.0.41,<3",
  "asyncpg>=0.30,<1",
  "alembic>=1.16,<2",
  "pgvector>=0.4,<1",
  "httpx>=0.28,<1",
  "python-multipart>=0.0.20,<1",
  "cryptography>=45,<46",
  "passlib[argon2]>=1.7,<2",
  "pyjwt>=2.10,<3",
  "redis>=6,<7",
]
worker = [
  "celery[redis]>=5.5,<6",
]
legacy = [
  "pyyaml>=6,<7",
  "chromadb>=1,<2",
  "sentence-transformers>=4,<6",
]
dev = [
  "pytest>=8.4,<9",
  "pytest-asyncio>=1,<2",
  "pytest-cov>=6,<7",
  "respx>=0.22,<1",
  "ruff>=0.12,<1",
  "mypy>=1.16,<2",
  "types-pyyaml>=6,<7",
]

[project.scripts]
proseforge = "proseforge.cli.main:main"
proseforge-legacy = "src.interfaces.cli:main"
```

- [ ] **Step 3: Add minimal API**

```python
from fastapi import FastAPI

def create_app() -> FastAPI:
    application = FastAPI(title="ProseForge API", version="1.0.0")
    return application

app = create_app()
```

- [ ] **Step 4: Add CLI delegation**

```python
from __future__ import annotations

import argparse

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="proseforge")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print("1.0.0.dev0")
    return 0
```

- [ ] **Step 5: Rebuild and test**

```bash
docker compose -f compose.yaml build proseforge
docker compose -f compose.yaml run --rm test -m pytest \
  tests/unit/test_package_entrypoints.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml proseforge tests/unit/test_package_entrypoints.py
git commit -m "build: add web v1 package entrypoints"
```

---

### Task 5: Define shared IDs, errors, results, and clocks

**Files:**
- Create: `proseforge/domain/common/ids.py`
- Create: `proseforge/domain/common/errors.py`
- Create: `proseforge/domain/common/result.py`
- Create: `proseforge/domain/ports/clock.py`
- Test: `tests/unit/domain/test_common_types.py`

**Interfaces:**
- Produces:
  - `new_id() -> str`
  - `DomainError`
  - `OperationResult[T]`
  - `Clock.now() -> datetime`

- [ ] **Step 1: Write tests**

```python
from datetime import UTC
from proseforge.domain.common.ids import new_id
from proseforge.domain.common.result import OperationResult

def test_new_id_is_lexically_sortable_string() -> None:
    first = new_id()
    second = new_id()
    assert isinstance(first, str)
    assert len(first) >= 20
    assert first < second

def test_operation_result_success() -> None:
    result = OperationResult.ok({"value": 1})
    assert result.success is True
    assert result.data == {"value": 1}
    assert result.errors == ()
```

- [ ] **Step 2: Implement IDs**

Use a monotonic ULID library or implement UUIDv7 through the standard library when available. Expose only:

```python
def new_id() -> str:
    ...
```

No other layer may generate IDs independently.

- [ ] **Step 3: Implement errors**

```python
class DomainError(Exception):
    code = "DOMAIN_ERROR"
    retryable = False

class NotFoundError(DomainError):
    code = "NOT_FOUND"

class ConflictError(DomainError):
    code = "CONFLICT"

class ValidationError(DomainError):
    code = "VALIDATION_ERROR"

class ProviderError(DomainError):
    code = "PROVIDER_ERROR"

class RetryableProviderError(ProviderError):
    code = "PROVIDER_RETRYABLE"
    retryable = True
```

- [ ] **Step 4: Implement generic result**

```python
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

@dataclass(frozen=True)
class OperationResult(Generic[T]):
    success: bool
    data: T | None
    warnings: tuple[dict[str, object], ...]
    errors: tuple[dict[str, object], ...]

    @classmethod
    def ok(cls, data: T) -> "OperationResult[T]":
        return cls(True, data, (), ())

    @classmethod
    def fail(
        cls,
        *,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> "OperationResult[T]":
        return cls(
            False,
            None,
            (),
            ({"code": code, "message": message, "retryable": retryable},),
        )
```

- [ ] **Step 5: Run tests and commit**

```bash
docker compose -f compose.yaml run --rm test -m pytest \
  tests/unit/domain/test_common_types.py -q
git add proseforge/domain tests/unit/domain
git commit -m "feat: add shared domain primitives"
```

---

### Task 6: Define the application ports

**Files:**
- Create: `proseforge/domain/ports/novel_engine.py`
- Create: `proseforge/domain/ports/model_provider.py`
- Create: `proseforge/domain/ports/blob_store.py`
- Create: `proseforge/domain/ports/repositories.py`
- Create: `proseforge/domain/ports/task_queue.py`
- Create: `proseforge/domain/ports/event_stream.py`
- Test: `tests/unit/domain/test_port_contracts.py`

**Interfaces:**
- Produces the only interfaces Application may depend on for external systems

- [ ] **Step 1: Write protocol shape tests**

```python
from typing import get_type_hints
from proseforge.domain.ports.model_provider import ModelProvider

def test_model_provider_defines_stream_and_list_models() -> None:
    assert hasattr(ModelProvider, "stream")
    assert hasattr(ModelProvider, "list_models")
```

- [ ] **Step 2: Implement `ModelProvider`**

```python
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

@dataclass(frozen=True)
class ProviderModel:
    provider: str
    model_id: str
    display_name: str
    capabilities: dict[str, object]
    context_window: int | None = None
    max_output_tokens: int | None = None

@dataclass(frozen=True)
class GenerationRequest:
    model: str
    system_blocks: tuple[dict[str, object], ...]
    input_blocks: tuple[dict[str, object], ...]
    response_schema: dict[str, object] | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    reasoning: dict[str, object] | None = None
    provider_options: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class GenerationEvent:
    event: str
    text: str = ""
    data: dict[str, object] = field(default_factory=dict)

class ModelProvider(Protocol):
    provider_id: str
    async def validate_credentials(self) -> dict[str, object]: ...
    async def list_models(self) -> list[ProviderModel]: ...
    async def count_tokens(self, request: GenerationRequest) -> int: ...
    async def stream(
        self,
        request: GenerationRequest,
    ) -> AsyncIterator[GenerationEvent]: ...
```

- [ ] **Step 3: Implement the exact `NovelEnginePort` from section 2.2**

- [ ] **Step 4: Implement `BlobStore`**

```python
class BlobStore(Protocol):
    async def put(self, *, data: bytes, media_type: str) -> str: ...
    async def get(self, storage_key: str) -> bytes: ...
    async def delete(self, storage_key: str) -> None: ...
    async def exists(self, storage_key: str) -> bool: ...
```

- [ ] **Step 5: Implement repository Unit of Work protocol**

```python
class UnitOfWork(Protocol):
    projects: ProjectRepository
    conversations: ConversationRepository
    messages: MessageRepository
    workflows: WorkflowRepository
    async def __aenter__(self) -> "UnitOfWork": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
```

Define repository method signatures in the same file. Do not expose SQLAlchemy model classes.

- [ ] **Step 6: Test and commit**

```bash
docker compose -f compose.yaml run --rm test -m pytest \
  tests/unit/domain/test_port_contracts.py -q
git add proseforge/domain/ports tests/unit/domain/test_port_contracts.py
git commit -m "feat: define application boundary ports"
```

---

### Task 7: Implement the only legacy-core adapter

**Files:**
- Create: `proseforge/infrastructure/legacy_engine/adapter.py`
- Create: `proseforge/infrastructure/legacy_engine/staging.py`
- Modify: `src/application/pipeline_service.py`
- Modify: `src/pipeline/post.py`
- Test: `tests/integration/legacy_engine/test_adapter.py`

**Interfaces:**
- Consumes: `NovelEnginePort`
- Produces: `LegacyNovelEngineAdapter`

- [ ] **Step 1: Write adapter test using a temporary legacy workspace**

```python
from proseforge.infrastructure.legacy_engine.adapter import LegacyNovelEngineAdapter

def test_adapter_rejects_commit_without_pre_state(legacy_project):
    adapter = LegacyNovelEngineAdapter(project_root=legacy_project.root)
    result = adapter.run_rule_quality(
        legacy_slot=legacy_project.slot,
        novel_slug=legacy_project.slug,
        novel_title=legacy_project.title,
        volume_no=1,
        chapter_no=1,
        chapter_type="normal",
        staged_file=str(legacy_project.chapter_file),
    )
    assert result.can_commit is False
    assert "pre_state" in " ".join(result.blocked_by)
```

- [ ] **Step 2: Implement staging helper**

Staging path:

```text
<project_root>/artifacts/web-staging/<project-id>/<chapter-id>/<run-id>/chapter.txt
```

Functions:

```python
def stage_text(*, root: Path, project_id: str, chapter_id: str, run_id: str, text: str) -> Path: ...
def cleanup_stage(path: Path) -> None: ...
```

Write through temporary file + `Path.replace()`.

- [ ] **Step 3: Implement Adapter**

Rules:

- Constructor receives explicit `project_root`.
- Every method receives explicit `legacy_slot`.
- No global active slot lookup.
- Convert old dict results to domain dataclasses.
- Capture old stdout into structured artifact logs.
- Never return `sqlite3.Row`.
- Catch only known legacy exceptions and map to Domain errors.
- `commit_chapter` checks content hash before invoking ingest.

- [ ] **Step 4: Change `src/pipeline/post.py`**

Split the current behavior so Adapter can execute:

```python
run_post_checks(..., ingest=False) -> dict
run_post_commit(..., precomputed_checks=dict) -> dict
```

Compatibility `run_post()` calls both.

The deterministic quality phase must not call any LLM or internal “agent review”.

- [ ] **Step 5: Run adapter and full tests**

```bash
docker compose -f compose.yaml run --rm test -m pytest \
  tests/integration/legacy_engine/test_adapter.py -q
docker compose -f compose.yaml run --rm test
```

- [ ] **Step 6: Commit**

```bash
git add proseforge/infrastructure/legacy_engine src/application src/pipeline/post.py tests/integration/legacy_engine
git commit -m "feat: isolate legacy novel engine behind adapter"
```

---

# Phase 3 — PostgreSQL, Persistence, and Legacy Import

### Task 8: Replace the development Compose topology

**Files:**
- Replace: `compose.yaml`
- Create: `compose.test.yaml`
- Create: `.env.example`
- Create: `docker/api.Dockerfile`
- Create: `docker/worker.Dockerfile`
- Create: `docker/web.Dockerfile`
- Create: `docker/entrypoint-api.sh`
- Create: `docker/entrypoint-worker.sh`
- Test: `tests/docker/test_compose_contract.py`

**Interfaces:**
- Produces services: `web`, `api`, `worker`, `scheduler`, `postgres`, `redis`, `legacy-test`, `api-test`, `web-test`

- [ ] **Step 1: Write Compose contract test**

Parse YAML and assert:

```python
def test_required_services_exist():
    services = load_compose()["services"]
    assert {
        "web", "api", "worker", "scheduler", "postgres", "redis"
    } <= set(services)
```

Also assert named volumes:

```text
postgres-data
proseforge-blobs
proseforge-backups
redis-data
model-cache
```

- [ ] **Step 2: Write `.env.example`**

```dotenv
PROSEFORGE_ENV=development
PROSEFORGE_PUBLIC_URL=http://localhost:3000
PROSEFORGE_DATABASE_URL=postgresql+asyncpg://proseforge:proseforge@postgres:5432/proseforge
PROSEFORGE_SYNC_DATABASE_URL=postgresql+psycopg://proseforge:proseforge@postgres:5432/proseforge
PROSEFORGE_REDIS_URL=redis://redis:6379/0
PROSEFORGE_BLOB_ROOT=/data/blobs
PROSEFORGE_BACKUP_ROOT=/data/backups
PROSEFORGE_MASTER_KEY=replace-with-32-byte-base64-key
PROSEFORGE_JWT_SECRET=replace-with-long-random-secret
PROSEFORGE_BOOTSTRAP_ADMIN_EMAIL=admin@example.local
PROSEFORGE_BOOTSTRAP_ADMIN_PASSWORD=change-me-now
```

- [ ] **Step 3: Replace Compose**

Required service properties:

- `postgres`: PostgreSQL 16 image with pgvector, healthcheck, volume.
- `redis`: Redis 7, AOF enabled, healthcheck.
- `api`: depends on healthy postgres/redis, mounts blobs/backups, no source bind in production profile.
- `worker`: same application image, Celery command.
- `scheduler`: Celery beat command.
- `web`: Nginx static image, port `3000:80`.
- `legacy-test`: build API image with legacy extras and run old tests.
- test services defined in `compose.test.yaml`.

- [ ] **Step 4: Add migration-safe API entrypoint**

`docker/entrypoint-api.sh`:

```bash
#!/usr/bin/env sh
set -eu
python -m proseforge.cli.main db wait
python -m proseforge.cli.main db migrate
exec "$@"
```

Use PostgreSQL advisory lock inside `db migrate`; do not rely on shell lock files.

- [ ] **Step 5: Validate Compose**

```bash
docker compose -f compose.yaml -f compose.test.yaml config >/tmp/proseforge-compose.txt
docker compose -f compose.yaml build postgres redis api worker
docker compose -f compose.yaml up -d postgres redis
docker compose -f compose.yaml ps
```

Expected: postgres and redis healthy.

- [ ] **Step 6: Commit**

```bash
git add compose.yaml compose.test.yaml .env.example docker tests/docker
git commit -m "build: add web service compose topology"
```

---

### Task 9: Add settings with strict secret validation

**Files:**
- Create: `proseforge/settings.py`
- Create: `tests/unit/test_settings.py`
- Modify: `proseforge/api/main.py`

**Interfaces:**
- Produces: cached `Settings` and explicit startup validation

- [ ] **Step 1: Write tests**

```python
import pytest
from pydantic import ValidationError
from proseforge.settings import Settings

def test_production_rejects_placeholder_secrets():
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://x",
            redis_url="redis://x",
            master_key="replace-with-32-byte-base64-key",
            jwt_secret="replace-with-long-random-secret",
        )
```

- [ ] **Step 2: Implement settings**

Use `BaseSettings`, prefix `PROSEFORGE_`, `extra="ignore"`.

Fields:

```text
environment
public_url
database_url
sync_database_url
redis_url
blob_root
backup_root
master_key
jwt_secret
bootstrap_admin_email
bootstrap_admin_password
max_upload_bytes
allowed_local_provider_hosts
```

- [ ] **Step 3: Add production validators**

Reject:

- placeholder strings
- JWT secret under 32 bytes
- invalid base64 master key
- master key that does not decode to 32 bytes
- relative blob/backup paths in production

- [ ] **Step 4: Inject settings into app factory**

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    app = FastAPI(...)
    app.state.settings = resolved
    return app
```

- [ ] **Step 5: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/unit/test_settings.py -q
git add proseforge/settings.py proseforge/api/main.py tests/unit/test_settings.py
git commit -m "feat: add strict application settings"
```

---

### Task 10: Add SQLAlchemy Unit of Work and Alembic foundation

**Files:**
- Create: `proseforge/infrastructure/database/base.py`
- Create: `proseforge/infrastructure/database/session.py`
- Create: `proseforge/infrastructure/database/uow.py`
- Create: `proseforge/infrastructure/database/models/__init__.py`
- Create: `alembic.ini`
- Create: `proseforge/infrastructure/database/migrations/env.py`
- Create: initial Alembic migration
- Test: `tests/integration/database/test_uow.py`

**Interfaces:**
- Produces:
  - `Base`
  - `create_engine_and_sessionmaker(settings)`
  - `SqlAlchemyUnitOfWork`

- [ ] **Step 1: Write rollback test**

```python
@pytest.mark.asyncio
async def test_uow_rolls_back_uncommitted_project(uow_factory):
    async with uow_factory() as uow:
        await uow.projects.add(Project.create(owner_id="u1", slug="book", title="Book"))
    async with uow_factory() as uow:
        assert await uow.projects.get_by_slug("u1", "book") is None
```

- [ ] **Step 2: Implement naming convention**

Use deterministic constraint names:

```python
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

- [ ] **Step 3: Implement async session and UoW**

- expire_on_commit=False
- one transaction per use case
- rollback on exception
- no global session
- repository objects created from the same session

- [ ] **Step 4: Add CLI database commands**

`proseforge cli`:

```text
db wait
db migrate
db current
```

`db migrate` obtains PostgreSQL advisory lock before Alembic upgrade.

- [ ] **Step 5: Test in PostgreSQL container**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm migration-test \
  pytest tests/integration/database/test_uow.py -q
```

- [ ] **Step 6: Commit**

```bash
git add alembic.ini proseforge/infrastructure/database proseforge/cli tests/integration/database
git commit -m "feat: add postgres unit of work and migrations"
```

---

### Task 11: Add core project, outline, volume, chapter, and version models

**Files:**
- Create: domain entities under `proseforge/domain/project/`, `outline/`, `chapter/`
- Create: database models under `proseforge/infrastructure/database/models/`
- Create: repositories under `proseforge/infrastructure/database/repositories/`
- Create: Alembic migration
- Test: `tests/integration/database/test_project_repository.py`
- Test: `tests/integration/database/test_chapter_versions.py`

**Interfaces:**
- Produces project and manuscript persistence

- [ ] **Step 1: Implement domain entities as dataclasses**

Example:

```python
@dataclass(frozen=True)
class Project:
    id: str
    owner_id: str
    slug: str
    title: str
    genre: str
    style: str
    language: str
    status: str
```

Do not expose ORM objects outside repositories.

- [ ] **Step 2: Create tables**

Exact tables:

```text
users
projects
project_members
outlines
outline_versions
volumes
chapters
chapter_versions
```

Constraints:

```text
users.email unique
projects(owner_id, slug) unique
outline_versions(outline_id, version_no) unique
volumes(project_id, volume_no) unique
chapters(project_id, chapter_no) unique
chapter_versions(chapter_id, version_no) unique
chapter_versions(chapter_id, content_hash) unique
```

- [ ] **Step 3: Write duplicate-content test**

```python
@pytest.mark.asyncio
async def test_same_content_hash_does_not_create_second_version(chapter_repo):
    first = await chapter_repo.append_version(chapter_id="c1", content="same")
    second = await chapter_repo.append_version(chapter_id="c1", content="same")
    assert second.id == first.id
    assert second.version_no == first.version_no
```

- [ ] **Step 4: Implement repositories**

Repository methods return domain dataclasses, not ORM classes.

- [ ] **Step 5: Migrate, test, commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm migration-test \
  alembic upgrade head
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/integration/database/test_project_repository.py \
         tests/integration/database/test_chapter_versions.py -q
git add proseforge alembic.ini tests/integration/database
git commit -m "feat: persist projects and manuscript versions"
```

---

### Task 12: Add conversation, branch, message, and stream chunk persistence

**Files:**
- Create: domain conversation entities
- Create: ORM models
- Create: repositories
- Create: migration
- Test: `tests/integration/database/test_conversation_branches.py`
- Test: `tests/integration/database/test_message_chunks.py`

**Interfaces:**
- Produces branch-aware durable conversations

- [ ] **Step 1: Add exact tables**

```text
conversations
conversation_branches
messages
message_chunks
conversation_events
```

Constraints:

```text
messages.client_request_id unique
message_chunks(message_id, chunk_index) unique
conversation_events(conversation_id, event_sequence) unique
```

- [ ] **Step 2: Write copy-on-write branch test**

```python
@pytest.mark.asyncio
async def test_branch_inherits_only_to_fork_point(conversation_repo):
    root = await fixture_conversation_with_three_messages(conversation_repo)
    branch = await conversation_repo.fork(
        conversation_id=root.id,
        forked_from_message_id=root.messages[1].id,
        name="Alternative",
    )
    visible = await conversation_repo.list_visible_messages(branch.id)
    assert [item.id for item in visible] == [
        root.messages[0].id,
        root.messages[1].id,
    ]
```

- [ ] **Step 3: Implement ancestry query**

Use a recursive CTE or deterministic parent traversal.  
Do not copy ancestor message rows.

- [ ] **Step 4: Implement chunk append atomically**

```python
async def append_chunk(
    *,
    message_id: str,
    chunk_index: int,
    event_type: str,
    content: str,
) -> None:
    ...
```

Duplicate `(message_id, chunk_index)` is idempotent.

- [ ] **Step 5: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/integration/database/test_conversation_branches.py \
         tests/integration/database/test_message_chunks.py -q
git add proseforge tests/integration/database
git commit -m "feat: persist branched conversations and streams"
```

---

### Task 13: Add provider, context, file, workflow, quality, health, and audit tables

**Files:**
- Create ORM/domain/repositories for remaining tables
- Create migration
- Test: `tests/integration/database/test_remaining_schema.py`

**Interfaces:**
- Produces complete durable schema

- [ ] **Step 1: Add exact tables**

```text
provider_credentials
model_catalog
model_profiles
attachments
artifacts
context_items
context_snapshots
embeddings
workflow_runs
workflow_steps
workflow_events
model_calls
quality_reports
health_checks
audit_logs
```

- [ ] **Step 2: Enable PostgreSQL extensions**

Migration:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

Downgrade does not drop extensions.

- [ ] **Step 3: Add required constraints**

```text
model_catalog(provider, model_id) unique
workflow_steps(workflow_run_id, idempotency_key) unique
workflow_events(workflow_run_id, sequence_no) unique
embeddings(project_id, source_type, source_id, chunk_index, embedding_model) unique
attachments.sha256 indexed
```

- [ ] **Step 4: Verify all migrations from empty database**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm migration-test sh -lc '
  alembic downgrade base &&
  alembic upgrade head &&
  pytest tests/integration/database/test_remaining_schema.py -q
'
```

- [ ] **Step 5: Commit**

```bash
git add proseforge/infrastructure/database tests/integration/database
git commit -m "feat: add web persistence schema"
```

---

### Task 14: Implement legacy SQLite workspace import

**Files:**
- Create: `proseforge/infrastructure/legacy_import/scanner.py`
- Create: `sqlite_reader.py`
- Create: `mapper.py`
- Create: `importer.py`
- Create: `verifier.py`
- Modify: `proseforge/cli/main.py`
- Test: `tests/migration/test_legacy_import.py`
- Test fixtures: `tests/fixtures/legacy_workspace/`

**Interfaces:**
- Produces: `proseforge migrate legacy --workspace <path>`

- [ ] **Step 1: Build a minimal fixture from current schema**

Fixture includes:

- one slot
- project.json
- novel.db
- one outline
- two chapters
- two versions of chapter 1
- one report artifact

- [ ] **Step 2: Write import preservation test**

```python
@pytest.mark.asyncio
async def test_legacy_import_preserves_latest_content_hash(importer, legacy_fixture):
    report = await importer.import_workspace(legacy_fixture)
    assert report.status == "COMPLETED"
    assert report.projects_imported == 1
    assert report.chapters_imported == 2
    assert report.hash_mismatches == ()
```

- [ ] **Step 3: Implement scanner**

Reject:

- symlink escapes
- missing project.json
- invalid registry entries

Do not mutate source files.

- [ ] **Step 4: Implement importer transaction per project**

Each slot imports in its own database transaction.  
A failed slot is reported as failed without rolling back successful independent slots.

- [ ] **Step 5: Copy source to read-only archive**

Archive root:

```text
/data/backups/legacy-import/<timestamp>/<slot>/
```

Only after the database transaction succeeds.

- [ ] **Step 6: Implement verifier**

Compare:

- project count
- chapter count
- latest content hash
- version count
- outline presence
- copied artifact hash

- [ ] **Step 7: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm migration-test \
  pytest tests/migration/test_legacy_import.py -q
git add proseforge/infrastructure/legacy_import proseforge/cli tests/migration tests/fixtures
git commit -m "feat: import legacy sqlite workspaces safely"
```

---

# Phase 4 — Native Model Provider Layer

### Task 15: Implement ProviderRegistry and normalized events

**Files:**
- Create: `proseforge/providers/registry.py`
- Create: `proseforge/providers/events.py`
- Create: `proseforge/providers/capabilities.py`
- Test: `tests/unit/providers/test_registry.py`
- Test: `tests/contract/providers/base_contract.py`

**Interfaces:**
- Produces:
  - `ProviderRegistry.register()`
  - `ProviderRegistry.get()`
  - normalized `GenerationEvent`

- [ ] **Step 1: Write registry tests**

```python
def test_duplicate_provider_id_is_rejected():
    registry = ProviderRegistry()
    registry.register(FakeProvider("fake"))
    with pytest.raises(ConflictError):
        registry.register(FakeProvider("fake"))
```

- [ ] **Step 2: Implement event enum**

Allowed events only:

```text
response.started
content.delta
reasoning.summary.delta
tool_call.started
tool_call.arguments.delta
tool_call.completed
usage.updated
response.completed
response.failed
```

- [ ] **Step 3: Implement capability validation**

Before dispatch, validate:

- input modality
- structured output
- tools
- reasoning controls
- max output
- context window

Raise `ModelCapabilityError` with exact missing capabilities.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/unit/providers tests/contract/providers/base_contract.py -q
git add proseforge/providers tests/unit/providers tests/contract/providers
git commit -m "feat: add native provider registry contract"
```

---

### Task 16: Encrypt provider credentials and protect custom endpoints

**Files:**
- Create: `proseforge/infrastructure/security/credential_cipher.py`
- Create: `proseforge/infrastructure/security/endpoint_policy.py`
- Create: `proseforge/application/providers/credential_service.py`
- Test: `tests/unit/security/test_credential_cipher.py`
- Test: `tests/unit/security/test_endpoint_policy.py`

**Interfaces:**
- Produces:
  - `CredentialCipher.encrypt/decrypt`
  - `EndpointPolicy.validate(url, allow_local=False)`

- [ ] **Step 1: Write encryption round-trip and tamper tests**

```python
def test_cipher_rejects_tampered_ciphertext(cipher):
    encrypted = bytearray(cipher.encrypt(b"secret"))
    encrypted[-1] ^= 1
    with pytest.raises(InvalidToken):
        cipher.decrypt(bytes(encrypted))
```

- [ ] **Step 2: Implement AES-256-GCM**

Stored payload:

```text
version byte + nonce + ciphertext + auth tag
```

Associated data includes:

```text
user_id + provider + credential_id
```

- [ ] **Step 3: Write endpoint policy tests**

Reject by default:

```text
http://
127.0.0.1
::1
169.254.169.254
link-local
RFC1918
URL userinfo
redirect to private network
```

Allow configured local hosts only when `allow_local=True`.

- [ ] **Step 4: Implement credential service**

Service returns masked keys only:

```text
sk-****abcd
```

No repository method returns plaintext credentials.

- [ ] **Step 5: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/unit/security -q
git add proseforge/infrastructure/security proseforge/application/providers tests/unit/security
git commit -m "feat: secure provider credentials and endpoints"
```

---

### Task 17: Implement the OpenAI native adapter

**Files:**
- Create: `proseforge/providers/openai.py`
- Create: `tests/contract/providers/test_openai_provider.py`
- Add official-doc metadata comment to adapter

**Interfaces:**
- Implements: `ModelProvider`
- Uses: OpenAI Models and Responses APIs
- Produces normalized events

- [ ] **Step 1: Write mocked list-models test**

Use `respx` to mock the official Models endpoint and assert:

- IDs are preserved exactly
- context/capability metadata is merged from local manifest only when vendor response omits it
- unknown models remain usable

- [ ] **Step 2: Write stream translation test**

Mock an event sequence and assert normalized events:

```text
response.started
content.delta
usage.updated
response.completed
```

- [ ] **Step 3: Implement adapter with `httpx.AsyncClient`**

Requirements:

- no vendor SDK required
- explicit timeout
- retry only transport/429/5xx through shared retry policy
- preserve response ID
- redact headers from errors
- send `response_schema` using current official structured-output field
- vendor options whitelist

- [ ] **Step 4: Add adapter metadata**

At file top:

```python
# Official docs: https://platform.openai.com/docs/api-reference
# Verified: 2026-07-15
# Model discovery: Models API
# Primary generation API: Responses API
```

At implementation time verify official docs again and update only when the official contract changed.

- [ ] **Step 5: Contract test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm contract-test \
  pytest tests/contract/providers/test_openai_provider.py -q
git add proseforge/providers/openai.py tests/contract/providers/test_openai_provider.py
git commit -m "feat: add OpenAI native provider"
```

---

### Task 18: Implement Anthropic, Google, and xAI adapters

**Files:**
- Create: `proseforge/providers/anthropic.py`
- Create: `proseforge/providers/google.py`
- Create: `proseforge/providers/xai.py`
- Create corresponding contract tests

**Interfaces:**
- Implements: `ModelProvider`
- Retains vendor-specific thinking, caching, and file capabilities

- [ ] **Step 1: Add one independent contract test per provider**

Each test covers:

- credential validation
- model listing
- text stream
- usage extraction
- structured output behavior
- provider error mapping

- [ ] **Step 2: Anthropic implementation**

Use official Messages API and Models API. Preserve:

```text
prompt caching
thinking/adaptive thinking options
content block types
stop reason
request ID
```

- [ ] **Step 3: Google implementation**

Use the official Gemini API/GenAI endpoint current on implementation date. Preserve:

```text
thinking controls
file input
cached content/context caching
model listing
safety settings
```

- [ ] **Step 4: xAI implementation**

Use the official xAI generation/Responses interface current on implementation date. Preserve:

```text
reasoning controls
search/tool capability flags
request ID
usage
```

- [ ] **Step 5: Run all contracts**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm contract-test \
  pytest tests/contract/providers/test_anthropic_provider.py \
         tests/contract/providers/test_google_provider.py \
         tests/contract/providers/test_xai_provider.py -q
```

- [ ] **Step 6: Commit**

```bash
git add proseforge/providers tests/contract/providers
git commit -m "feat: add Anthropic Google and xAI providers"
```

---

### Task 19: Implement domestic native providers

**Files:**
- Create:
  - `proseforge/providers/deepseek.py`
  - `kimi.py`
  - `dashscope.py`
  - `zhipu.py`
  - `volcengine.py`
  - `baidu.py`
  - `tencent.py`
  - `minimax.py`
- Create one contract test per provider

**Interfaces:**
- Implements: `ModelProvider`
- Uses each vendor’s official endpoint and authentication scheme

- [ ] **Step 1: Build shared HTTP helpers without forcing one protocol**

Shared helper may provide:

- timeout
- retry
- redaction
- SSE parsing
- JSON parsing

It must not assume OpenAI request/response shape.

- [ ] **Step 2: Implement each adapter independently**

For every adapter:

- record official documentation URL
- record verification date
- implement credential probe
- implement model discovery when vendor supports it
- otherwise use versioned manifest + manual model ID
- preserve regional endpoints
- preserve thinking/reasoning options
- normalize events
- map auth/rate/validation/server errors

- [ ] **Step 3: Do not claim model coverage through hardcoding**

Tests must prove an unknown model ID returned by a vendor listing is accepted and stored.

- [ ] **Step 4: Run contracts**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm contract-test \
  pytest tests/contract/providers/test_deepseek_provider.py \
         tests/contract/providers/test_kimi_provider.py \
         tests/contract/providers/test_dashscope_provider.py \
         tests/contract/providers/test_zhipu_provider.py \
         tests/contract/providers/test_volcengine_provider.py \
         tests/contract/providers/test_baidu_provider.py \
         tests/contract/providers/test_tencent_provider.py \
         tests/contract/providers/test_minimax_provider.py -q
```

- [ ] **Step 5: Commit**

```bash
git add proseforge/providers tests/contract/providers
git commit -m "feat: add domestic native model providers"
```

---

### Task 20: Implement Mistral, Cohere, Ollama, vLLM, and compatible endpoints

**Files:**
- Create provider files and contract tests
- Modify: provider registry bootstrap

**Interfaces:**
- Provides hosted and local/custom model support

- [ ] **Step 1: Implement Mistral and Cohere natively**

Do not route through generic OpenAI compatibility when native APIs expose extra capabilities required by the product.

- [ ] **Step 2: Implement Ollama**

Default permitted URL only:

```text
http://ollama:11434
```

Additional hosts require allowlist.

- [ ] **Step 3: Implement vLLM**

Default permitted URL only:

```text
http://vllm:8000
```

- [ ] **Step 4: Implement generic compatibility adapters**

Separate:

```text
OpenAICompatibleProvider
AnthropicCompatibleProvider
```

UI marks these as `CUSTOM`, not native.

- [ ] **Step 5: Contract tests and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm contract-test \
  pytest tests/contract/providers -q
git add proseforge/providers tests/contract/providers
git commit -m "feat: add hosted local and compatible providers"
```

---

### Task 21: Implement dynamic model catalog synchronization

**Files:**
- Create: `proseforge/application/providers/model_catalog_service.py`
- Create: `proseforge/workflows/tasks/model_catalog.py`
- Create: `proseforge/providers/manifests/*.json`
- Test: `tests/integration/providers/test_model_catalog_sync.py`

**Interfaces:**
- Produces:
  - manual sync
  - scheduled sync
  - soft deprecation
  - profile-safe model retention

- [ ] **Step 1: Write unknown-model discovery test**

```python
@pytest.mark.asyncio
async def test_sync_adds_new_vendor_model_without_code_change(service, fake_provider):
    fake_provider.models = [ProviderModel(provider="fake", model_id="future-model", display_name="Future", capabilities={})]
    result = await service.sync("credential-1")
    assert result.added == ("future-model",)
```

- [ ] **Step 2: Implement sync merge rules**

- vendor listing is authoritative for existence at sync time
- local manifest supplements capabilities
- manual entries are not deleted
- missing vendor models become `UNAVAILABLE`, not deleted
- models referenced by profiles remain visible
- sync records timestamp and error

- [ ] **Step 3: Add Celery scheduled task**

Default cadence:

```text
daily at 03:00 local server time
```

Manual sync remains available.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/integration/providers/test_model_catalog_sync.py -q
git add proseforge/application/providers proseforge/workflows/tasks proseforge/providers/manifests tests/integration/providers
git commit -m "feat: synchronize dynamic model catalog"
```

---

# Phase 5 — Context Engine

### Task 22: Add token budgeting and tokenizer abstraction

**Files:**
- Create: `proseforge/context_engine/tokenizer.py`
- Create: `proseforge/context_engine/budgeting.py`
- Test: `tests/unit/context/test_budgeting.py`

**Interfaces:**
- Produces:
  - `Tokenizer.count(text) -> int`
  - `ContextBudget.calculate(...)`

- [ ] **Step 1: Write exact budget test**

```python
def test_budget_reserves_output_provider_and_margin():
    budget = calculate_budget(
        context_window=100_000,
        requested_output=10_000,
        provider_reserved=2_000,
        safety_margin_ratio=0.10,
    )
    assert budget.input_tokens == 78_000
```

- [ ] **Step 2: Implement tokenizers**

Resolution:

1. provider-native count endpoint
2. provider tokenizer library
3. conservative fallback estimator

Fallback must overestimate Chinese text rather than underestimate.

- [ ] **Step 3: Implement category allocation**

Categories:

```text
system
workflow
canon
outline
chapter
recent_messages
retrieval
memory
references
output_reserve
```

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/unit/context/test_budgeting.py -q
git add proseforge/context_engine tests/unit/context
git commit -m "feat: add context token budgeting"
```

---

### Task 23: Add context items, snapshots, provenance, and compiler

**Files:**
- Create: `compiler.py`
- Create: `provenance.py`
- Create: application context service
- Test: `tests/integration/context/test_context_compiler.py`

**Interfaces:**
- Produces `CompiledContext`

```python
@dataclass(frozen=True)
class CompiledContext:
    snapshot_id: str
    blocks: tuple[dict[str, object], ...]
    estimated_tokens: int
    source_ids: tuple[str, ...]
    excluded_ids: tuple[str, ...]
    compiler_version: str
```

- [ ] **Step 1: Write priority test**

Pinned canon and current chapter plan must survive a constrained budget before old summaries and optional references.

- [ ] **Step 2: Implement deterministic ordering**

Order:

```text
system
workflow constraints
pinned canon
current outline/volume/chapter
previous chapter tail
recent branch messages
retrieved original chunks
structured memory
old summaries
optional references
```

- [ ] **Step 3: Save immutable snapshot**

Snapshot stores:

- item IDs
- source hashes
- token estimates
- exclusions
- model ID
- compiler version

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/integration/context/test_context_compiler.py -q
git add proseforge/context_engine proseforge/application/context tests/integration/context
git commit -m "feat: compile traceable context snapshots"
```

---

### Task 24: Add local deduplication, retrieval, compaction, and validation

**Files:**
- Create: `deduplication.py`
- Create: `retrieval.py`
- Create: `compaction.py`
- Create: `validation.py`
- Create prompt: `proseforge/prompts/compressor/context_summary.v1.yaml`
- Test: `tests/integration/context/test_compaction.py`
- Test: `tests/integration/context/test_branch_isolation.py`

**Interfaces:**
- Produces reversible high-fidelity context virtualization

- [ ] **Step 1: Write raw-source preservation test**

```python
@pytest.mark.asyncio
async def test_compaction_never_deletes_source_messages(service, long_conversation):
    before = await service.message_count(long_conversation.id)
    await service.compact(long_conversation.branch_id)
    after = await service.message_count(long_conversation.id)
    assert after == before
```

- [ ] **Step 2: Implement local dedup**

Use normalized content hash.  
Never merge two blocks with different source ranges even if text matches unless both source references are retained.

- [ ] **Step 3: Implement structured summary schema**

Fields:

```text
facts
decisions
constraints
characters
timeline
open_questions
unresolved_plot_threads
style_requirements
source_message_ids
```

- [ ] **Step 4: Implement vector retrieval**

Use pgvector with project filter and source visibility filter.  
Branch queries must not retrieve sibling-branch private messages.

- [ ] **Step 5: Implement Story Contract validator**

Return:

```text
PASS
WARN
BLOCK
```

BLOCK causes compiler fallback to original chunks.

- [ ] **Step 6: Test branch isolation and fallback**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/integration/context/test_compaction.py \
         tests/integration/context/test_branch_isolation.py -q
```

- [ ] **Step 7: Commit**

```bash
git add proseforge/context_engine proseforge/prompts tests/integration/context
git commit -m "feat: add reversible context compaction"
```

---

# Phase 6 — Files and Conversation Application Services

### Task 25: Implement LocalBlobStore and secure upload parsing

**Files:**
- Create: `proseforge/infrastructure/blob/local.py`
- Create: `proseforge/application/files/upload_service.py`
- Create: `proseforge/application/files/parser_service.py`
- Create parsers for txt/md/json/yaml/docx/pdf/epub/csv/zip
- Test: `tests/integration/files/`
- Test: `tests/security/test_upload_security.py`

**Interfaces:**
- Produces content-addressed files and parsed attachments

- [ ] **Step 1: Write content-addressing test**

Same bytes uploaded twice produce the same storage key but separate attachment rows.

- [ ] **Step 2: Implement storage path**

```text
/data/blobs/sha256/<first-2>/<next-2>/<sha256>
```

Write temp file, fsync, atomic replace.

- [ ] **Step 3: Implement upload limits**

Validate:

- MIME sniffing
- max bytes
- total quota
- safe filename
- path traversal
- zip entry count
- zip expansion ratio
- PDF page count
- symlink entries

- [ ] **Step 4: Handle scanned PDF**

When no text layer:

```text
parse_status=OCR_REQUIRED
```

Do not return empty success.

- [ ] **Step 5: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/integration/files tests/security/test_upload_security.py -q
git add proseforge/infrastructure/blob proseforge/application/files tests/integration/files tests/security
git commit -m "feat: add secure durable file storage"
```

---

### Task 26: Implement conversation and branch use cases

**Files:**
- Create: `proseforge/application/conversations/create_conversation.py`
- Create: `send_message.py`
- Create: `fork_branch.py`
- Create: `list_messages.py`
- Test: `tests/unit/application/test_conversation_use_cases.py`

**Interfaces:**
- Produces:
  - `CreateConversation`
  - `ForkBranch`
  - `ListVisibleMessages`
  - pre-model message persistence

- [ ] **Step 1: Write duplicate client request test**

Two `SendMessage` calls with the same `client_request_id` return the same stored message.

- [ ] **Step 2: Implement send transaction**

Transaction creates:

1. user message COMPLETED
2. assistant message PENDING
3. conversation event

Commit before queueing model generation.

- [ ] **Step 3: Implement branch fork**

Fork requires a message in the same conversation.  
Fork snapshot points to the fork message.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/unit/application/test_conversation_use_cases.py -q
git add proseforge/application/conversations tests/unit/application
git commit -m "feat: add durable conversation use cases"
```

---

### Task 27: Implement streamed chat generation and recovery

**Files:**
- Create: `proseforge/application/conversations/generate_reply.py`
- Create: `proseforge/workflows/tasks/chat.py`
- Create: `proseforge/application/conversations/recover_partial.py`
- Test: `tests/integration/conversations/test_stream_recovery.py`

**Interfaces:**
- Consumes: ProviderRegistry, ContextCompiler, UnitOfWork, EventStream
- Produces: durable streaming assistant messages

- [ ] **Step 1: Write interrupted-stream test**

Simulate provider yielding two deltas then raising a transport error.

Assert:

```text
assistant status = PARTIAL
two chunks persisted
recovery starts at next chunk index
user message is not duplicated
```

- [ ] **Step 2: Implement stream order**

1. load PENDING assistant message
2. compile context
3. create model_call
4. mark STREAMING
5. append chunk in DB
6. publish normalized event
7. on complete merge chunks and mark COMPLETED
8. on cancel mark CANCELLED
9. on interruption mark PARTIAL
10. on non-retryable failure mark FAILED

- [ ] **Step 3: Add recovery command**

`Continue` creates a new model call linked to the partial message and sends existing partial text as continuation context without rewriting stored chunks.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/integration/conversations/test_stream_recovery.py -q
git add proseforge/application/conversations proseforge/workflows/tasks/chat.py tests/integration/conversations
git commit -m "feat: stream and recover chat replies"
```

---

# Phase 7 — Durable Novel Workflow

### Task 28: Implement workflow state machine, event log, leases, and checkpoints

**Files:**
- Create: `proseforge/domain/workflow/state.py`
- Create: `proseforge/application/workflows/service.py`
- Create: `proseforge/workflows/engine.py`
- Create: `proseforge/workflows/recovery.py`
- Create: `proseforge/workflows/tasks/base.py`
- Test: `tests/integration/workflows/test_state_machine.py`
- Test: `tests/recovery/test_worker_restart.py`

**Interfaces:**
- Produces durable workflow execution

- [ ] **Step 1: Define allowed transitions**

```python
ALLOWED_TRANSITIONS = {
    "CREATED": {"WAITING_USER", "QUEUED", "CANCELLED"},
    "WAITING_USER": {"QUEUED", "CANCELLED"},
    "QUEUED": {"RUNNING", "CANCELLED"},
    "RUNNING": {"PAUSED", "RETRYING", "COMPLETED", "FAILED", "CANCELLED", "RECOVERING"},
    "PAUSED": {"QUEUED", "CANCELLED"},
    "RETRYING": {"RUNNING", "FAILED", "PAUSED"},
    "RECOVERING": {"QUEUED", "PAUSED", "FAILED"},
    "COMPLETED": set(),
    "FAILED": {"QUEUED"},
    "CANCELLED": set(),
}
```

- [ ] **Step 2: Write illegal transition test**

`COMPLETED -> RUNNING` must raise `InvalidWorkflowTransition`.

- [ ] **Step 3: Implement lease**

Fields:

```text
lease_owner
lease_expires_at
heartbeat_at
```

A task may run only after atomic lease acquisition.

- [ ] **Step 4: Implement checkpoint**

Checkpoint after each externally observable side effect:

- model call completed
- staged draft saved
- review saved
- chapter committed
- export created

- [ ] **Step 5: Implement recovery**

Expired RUNNING lease becomes RECOVERING, then QUEUED from last incomplete idempotent step.

- [ ] **Step 6: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm recovery-test \
  pytest tests/integration/workflows/test_state_machine.py \
         tests/recovery/test_worker_restart.py -q
git add proseforge/domain/workflow proseforge/application/workflows proseforge/workflows tests/integration/workflows tests/recovery
git commit -m "feat: add durable workflow engine"
```

---

### Task 29: Implement outline intake and clarification

**Files:**
- Create: `proseforge/application/outlines/intake_service.py`
- Create: `proseforge/workflows/outline_intake.py`
- Create prompts:
  - `outline/parse.v1.yaml`
  - `outline/clarify.v1.yaml`
  - `outline/confirm.v1.yaml`
- Test: `tests/integration/workflows/test_outline_intake.py`

**Interfaces:**
- Produces parsed outline and explicit execution confirmation

- [ ] **Step 1: Define parse schema**

```text
title
genre
style
protagonist
characters
worldbuilding
core_conflict
planned_volumes
planned_chapters
chapter_word_target
point_of_view
ending_direction
prohibitions
missing_required_fields
```

- [ ] **Step 2: Write minimal-question test**

When title, genre, characters, and POV exist but volume/chapter counts do not, only ask:

```text
计划写多少卷？
每卷多少章，或全书总章数？
单章大约多少字？
是否允许自动改写？
最大自动改写轮数？
是否每章完成后暂停？
```

- [ ] **Step 3: Implement WAITING_USER**

Workflow cannot begin generation until confirmation card is accepted.

- [ ] **Step 4: Save outline version before planning**

Store raw attachment, raw extracted text, parsed JSON, prompt version, model call ID, content hash.

- [ ] **Step 5: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/integration/workflows/test_outline_intake.py -q
git add proseforge/application/outlines proseforge/workflows/outline_intake.py proseforge/prompts/outline tests/integration/workflows
git commit -m "feat: add outline intake workflow"
```

---

### Task 30: Implement volume and chapter planning

**Files:**
- Create: `proseforge/application/writing/planning_service.py`
- Create: `proseforge/workflows/novel_planning.py`
- Create prompts:
  - `planner/volume_plan.v1.yaml`
  - `planner/chapter_plan.v1.yaml`
- Test: `tests/integration/workflows/test_novel_planning.py`

**Interfaces:**
- Produces validated volume/chapter plan rows

- [ ] **Step 1: Define planning output schema**

Each chapter:

```text
chapter_no
volume_no
title
chapter_type
goal
main_event
conflict
characters
plot_threads_to_advance
canon_constraints
ending_hook
target_words
```

- [ ] **Step 2: Validate coverage**

Reject plan when:

- duplicate chapter numbers
- missing chapter number
- volume range mismatch
- target words outside policy
- referenced character absent without explicit introduction
- required ending direction omitted

- [ ] **Step 3: Save plans transactionally**

All plans for one planning revision are inserted in one transaction.  
Failure does not leave a partial plan active.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/integration/workflows/test_novel_planning.py -q
git add proseforge/application/writing proseforge/workflows/novel_planning.py proseforge/prompts/planner tests/integration/workflows
git commit -m "feat: add validated novel planning"
```

---

### Task 31: Implement rule QualityService around existing Guards

**Files:**
- Create: `proseforge/application/quality/rule_quality_service.py`
- Create: `proseforge/infrastructure/legacy_engine/quality_adapter.py`
- Modify: `src/guards/guard_registry.py`
- Modify: `src/pipeline/post.py`
- Test: `tests/integration/quality/test_rule_quality_service.py`

**Interfaces:**
- Produces one `QualityDecision`

```python
@dataclass(frozen=True)
class QualityDecision:
    status: str
    can_commit: bool
    blocked_by: tuple[str, ...]
    warnings: tuple[dict[str, object], ...]
    report: dict[str, object]
```

- [ ] **Step 1: Write crash-is-not-pass test**

A crashing L1 Guard results in:

```text
status=ERROR
can_commit=False
```

- [ ] **Step 2: Normalize decisions**

Rules:

```text
L1 FAIL -> BLOCK
L3 FAIL -> BLOCK
Guard crash -> ERROR/BLOCK
L2 FAIL -> WARN unless project policy escalates
human texture -> policy-controlled
```

- [ ] **Step 3: Remove print-driven decision logic**

Legacy code may still print compatibility logs, but Application must consume a returned `QualityDecision`.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/integration/quality/test_rule_quality_service.py -q
docker compose -f compose.yaml run --rm legacy-test
git add proseforge/application/quality proseforge/infrastructure/legacy_engine src/guards src/pipeline/post.py tests/integration/quality
git commit -m "feat: expose deterministic quality decisions"
```

---

### Task 32: Implement writer generation, model review, rewrite, and final commit

**Files:**
- Create: `proseforge/workflows/chapter_generation.py`
- Create: `proseforge/application/writing/chapter_service.py`
- Create: `proseforge/application/quality/model_review_service.py`
- Create prompts:
  - `writer/chapter.v1.yaml`
  - `reviewer/chapter_review.v1.yaml`
  - `rewriter/chapter_rewrite.v1.yaml`
- Test: `tests/integration/workflows/test_chapter_generation.py`
- Test: `tests/integration/workflows/test_rewrite_limit.py`

**Interfaces:**
- Consumes Writer profile, Editor profile, ContextCompiler, QualityService, NovelEnginePort
- Produces committed chapter version or blocked workflow

- [ ] **Step 1: Define review JSON Schema**

Use:

```json
{
  "status": "PASS | WARN | BLOCK",
  "summary": "string",
  "issues": [
    {
      "id": "string",
      "severity": "low | medium | high | critical",
      "category": "continuity | character | plot | prose | pacing | canon | style",
      "evidence": [{"start": 0, "end": 1, "quote": "string"}],
      "explanation": "string",
      "rewrite_instruction": "string",
      "must_fix": true
    }
  ],
  "preserve": ["string"],
  "rewrite_scope": [{"start": 0, "end": 1}]
}
```

- [ ] **Step 2: Implement staged draft**

Writer output stored as artifact and draft version.  
It is not `chapters.active_version_id`.

- [ ] **Step 3: Run rule review then model review**

Merged decision:

```text
Rule BLOCK -> BLOCK
Rule ERROR -> BLOCK
Model critical issue -> BLOCK
Rule WARN or Model WARN -> WARN
Both PASS -> PASS
```

- [ ] **Step 4: Implement rewrite loop**

Default:

```text
writer profile = drafting
editor profile = review + rewrite
```

Loop stops when:

- PASS
- max rewrite rounds reached
- non-retryable provider failure
- large rewrite block
- user cancellation

- [ ] **Step 5: Implement diff validation**

Reuse or port:

```text
src/pipeline/revision_diff_report.py
src/pipeline/semantic_contract.py
```

No direct import outside legacy adapter.  
Expose domain result containing changed ratio, preserved facts, regressions.

- [ ] **Step 6: Commit only after pass**

Call `NovelEnginePort.commit_chapter()` and repository append version in an idempotent transaction.  
Set active version only after both complete. If cross-store finalization fails, workflow becomes `RECOVERING`, not `COMPLETED`.

- [ ] **Step 7: Test restart and rewrite limit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm recovery-test \
  pytest tests/integration/workflows/test_chapter_generation.py \
         tests/integration/workflows/test_rewrite_limit.py -q
```

- [ ] **Step 8: Commit**

```bash
git add proseforge/workflows/chapter_generation.py proseforge/application/writing proseforge/application/quality proseforge/prompts tests/integration/workflows
git commit -m "feat: automate write review rewrite and commit"
```

---

### Task 33: Implement whole-novel orchestration

**Files:**
- Create: `proseforge/workflows/novel_generation.py`
- Create: `proseforge/application/workflows/novel_workflow_service.py`
- Test: `tests/integration/workflows/test_whole_novel.py`

**Interfaces:**
- Produces sequential durable chapter execution with pause/cancel/resume

- [ ] **Step 1: Write three-chapter recovery test**

Stop worker during chapter 2. After recovery:

- chapter 1 has one active version
- chapter 2 resumes
- chapter 1 is not regenerated
- chapter 3 starts only after chapter 2 completes

- [ ] **Step 2: Implement orchestration**

Per chapter:

```text
plan
context
draft
rule-check
model-review
rewrite
final-validation
commit
index
checkpoint
```

- [ ] **Step 3: Implement user controls**

```text
pause now
pause after current chapter
resume
cancel unstarted work
retry failed step
branch from chapter
```

- [ ] **Step 4: Implement cost guard**

Before each model call:

- estimate tokens
- estimate price when known
- compare project/run limit
- pause if limit exceeded
- do not silently switch provider

- [ ] **Step 5: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm recovery-test \
  pytest tests/integration/workflows/test_whole_novel.py -q
git add proseforge/workflows/novel_generation.py proseforge/application/workflows tests/integration/workflows
git commit -m "feat: orchestrate durable whole novel generation"
```

---

# Phase 8 — FastAPI and SSE

### Task 34: Add API error mapping, authentication, and health

**Files:**
- Create: `proseforge/api/errors.py`
- Create: `proseforge/api/routes/auth.py`
- Create: `proseforge/api/routes/health.py`
- Create: `proseforge/application/health/service.py`
- Modify: `proseforge/api/main.py`
- Test: `tests/api/test_auth.py`
- Test: `tests/api/test_health.py`

**Interfaces:**
- Produces JWT session auth and liveness/readiness

- [ ] **Step 1: Define error response**

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Human-readable message",
    "retryable": false,
    "request_id": "id",
    "details": {}
  }
}
```

- [ ] **Step 2: Implement bootstrap admin**

Only runs when users table is empty.  
Production rejects default password.

- [ ] **Step 3: Implement health checks**

```text
GET /api/v1/health/live
GET /api/v1/health/ready
GET /api/v1/health/report
POST /api/v1/health/run
```

Readiness checks PostgreSQL, migrations, Redis, BlobStore, key, pgvector.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/api/test_auth.py tests/api/test_health.py -q
git add proseforge/api proseforge/application/health tests/api
git commit -m "feat: add API auth errors and health"
```

---

### Task 35: Add project, outline, provider, model, context, chapter, and file routes

**Files:**
- Create route modules and Pydantic schemas
- Test route modules under `tests/api/`

**Interfaces:**
- Produces REST `/api/v1` resources

- [ ] **Step 1: Add routes exactly**

```text
/projects
/outlines
/providers
/models
/model-profiles
/context
/chapters
/files
/exports
```

- [ ] **Step 2: Enforce route boundary**

Each route:

```python
@router.post(...)
async def endpoint(
    request: RequestSchema,
    use_case: Annotated[UseCase, Depends(get_use_case)],
) -> ResponseSchema:
    result = await use_case.execute(request.to_command())
    return ResponseSchema.from_result(result)
```

No Session argument in route signatures.

- [ ] **Step 3: Add ownership tests**

A user cannot read another user’s project, attachment, conversation, model credential, or export.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/api -q
git add proseforge/api tests/api
git commit -m "feat: expose project and configuration APIs"
```

---

### Task 36: Add conversation, branch, message, workflow, and SSE routes

**Files:**
- Create: conversation/workflow routes
- Create: `proseforge/api/sse/encoder.py`
- Create: `proseforge/api/sse/stream.py`
- Test: `tests/api/test_sse_reconnect.py`

**Interfaces:**
- Produces durable SSE with `Last-Event-ID`

- [ ] **Step 1: Add endpoints**

```text
GET/POST /conversations
GET/PATCH/DELETE /conversations/{id}
GET/POST /conversations/{id}/messages
POST /conversations/{id}/branches
POST /messages/{id}/stop
POST /messages/{id}/retry
POST /projects/{id}/workflows/novel
GET /workflows/{id}
GET /workflows/{id}/events
POST /workflows/{id}/pause
POST /workflows/{id}/resume
POST /workflows/{id}/cancel
POST /workflows/{id}/retry
```

- [ ] **Step 2: Implement SSE encoder**

```python
def encode_sse(*, event_id: str, event: str, data: dict[str, object]) -> bytes:
    payload = orjson.dumps(data).decode()
    return f"id: {event_id}\nevent: {event}\ndata: {payload}\n\n".encode()
```

- [ ] **Step 3: Reconnect from durable event log**

When `Last-Event-ID` exists, query later events from PostgreSQL before subscribing to live PubSub.

- [ ] **Step 4: Test disconnect/reconnect**

No duplicate event IDs and no missing deltas.

- [ ] **Step 5: Commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test \
  pytest tests/api/test_sse_reconnect.py -q
git add proseforge/api tests/api
git commit -m "feat: expose durable chat and workflow streams"
```

---

# Phase 9 — React Web

### Task 37: Scaffold React SPA and design tokens

**Files:**
- Create full `apps/web` scaffold
- Create `apps/web/src/styles/tokens.css`
- Create `apps/web/src/app/router.tsx`
- Create `apps/web/src/lib/api/`
- Test: `apps/web/src/app/app.test.tsx`

**Interfaces:**
- Produces warm responsive SPA

- [ ] **Step 1: Initialize with pnpm**

```bash
corepack enable
pnpm create vite apps/web --template react-ts
cd apps/web
pnpm add @tanstack/react-router @tanstack/react-query zustand dexie \
  react-markdown @tiptap/react @tiptap/starter-kit zod
pnpm add -D vitest @testing-library/react @testing-library/jest-dom \
  @playwright/test eslint prettier
```

Commit generated lockfile.

- [ ] **Step 2: Add exact warm tokens**

```css
:root {
  --background: #fff8ee;
  --surface: #fffdf8;
  --surface-muted: #f7ebdd;
  --surface-strong: #f0ddc8;
  --text-primary: #3b2a22;
  --text-secondary: #725b4d;
  --text-muted: #9a8172;
  --primary: #c96f45;
  --primary-hover: #b85f39;
  --primary-soft: #f7d7c3;
  --accent: #d69a45;
  --success: #4f8a69;
  --warning: #b77a24;
  --danger: #b6534f;
  --border: #e7d5c3;
  --focus: #d8865c;
}
```

- [ ] **Step 3: Add routes**

```text
/login
/app/projects
/app/projects/$projectId/chat/$conversationId
/app/projects/$projectId/write
/app/projects/$projectId/outline
/app/projects/$projectId/context
/app/projects/$projectId/workflows
/app/projects/$projectId/exports
/app/settings/providers
/app/settings/models
/app/settings/storage
/app/settings/health
```

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test \
  pnpm test -- --run
git add apps/web
git commit -m "feat: scaffold warm ProseForge web app"
```

---

### Task 38: Implement authentication and project shell

**Files:**
- Create auth feature
- Create project sidebar
- Create responsive shell
- Test component behavior

**Interfaces:**
- Produces authenticated app and project navigation

- [ ] **Step 1: Add login test**

Successful login stores only session token according to chosen cookie strategy. Prefer secure HttpOnly cookie; frontend does not persist raw token.

- [ ] **Step 2: Implement desktop shell**

```text
left project/conversation sidebar
center route outlet
right optional context/workflow panel
```

- [ ] **Step 3: Implement mobile shell**

Sidebar drawer and bottom sheet; editor can go full screen.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test \
  pnpm test -- --run
git add apps/web/src
git commit -m "feat: add authenticated project shell"
```

---

### Task 39: Implement provider and model settings UI

**Files:**
- Create provider settings feature
- Create model profile feature
- Test masking and capability display

**Interfaces:**
- Produces Writer/Editor configuration

- [ ] **Step 1: Build provider form**

Fields:

```text
provider
display name
API key
base URL when supported
region
local endpoint permission
```

Never prefill secret.

- [ ] **Step 2: Add connection test UI**

Show:

- healthy/unhealthy
- latency
- discovered models
- last check
- redacted error

- [ ] **Step 3: Add model profile editor**

Roles:

```text
writer
editor
reviewer
planner
compressor
embedding
```

Writer and Editor required per project; optional roles inherit.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test \
  pnpm test -- --run
git add apps/web/src/features/providers apps/web/src/features/settings
git commit -m "feat: add provider and model profile settings"
```

---

### Task 40: Implement GPT-style chat, files, and branches

**Files:**
- Create chat feature components/hooks/stores
- Create IndexedDB draft store
- Test: component tests and Playwright branch test

**Interfaces:**
- Produces ChatGPT-like conversation surface

- [ ] **Step 1: Build message input**

Features:

- multiline
- drag/drop
- paste image
- upload progress
- send
- stop
- retry
- branch
- workflow mode toggle

- [ ] **Step 2: Implement SSE hook**

Reconnect with last event ID.  
Deduplicate by event ID.  
Render partial content without losing persisted state.

- [ ] **Step 3: Implement branch selector**

Each assistant message exposes:

```text
重新生成
从这里分支
查看其他回答
```

- [ ] **Step 4: Add IndexedDB drafts**

Keys:

```text
conversation_id
branch_id
draft_type
```

Restore after refresh.

- [ ] **Step 5: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test \
  pnpm test -- --run
git add apps/web/src/features/chat apps/web/src/lib/indexed-db
git commit -m "feat: add branched streaming chat"
```

---

### Task 41: Implement context and workflow panels

**Files:**
- Create context feature
- Create workflow feature
- Test interactions

**Interfaces:**
- Produces token budget and durable workflow visibility

- [ ] **Step 1: Context panel**

Show:

- model context window
- used tokens
- output reserve
- layers
- pinned items
- summaries
- retrieved originals
- excluded items
- provenance
- validation status

Operations:

```text
pin
unpin
priority
exclude
edit memory
recompact
restore source
download snapshot
```

- [ ] **Step 2: Workflow panel**

Show:

- current step
- completed steps
- chapter progress
- retry count
- model
- token/cost estimate
- pause/resume/cancel/retry

- [ ] **Step 3: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test \
  pnpm test -- --run
git add apps/web/src/features/context apps/web/src/features/workflow
git commit -m "feat: add context and workflow control panels"
```

---

### Task 42: Implement writing studio, review issue navigation, and diff

**Files:**
- Create writing feature
- Add Tiptap editor
- Add version and diff views
- Test autosave and conflict handling

**Interfaces:**
- Produces chapter writing and version management

- [ ] **Step 1: Build three-column layout**

```text
chapter tree | editor | review/diff
```

- [ ] **Step 2: Implement autosave**

- debounce 800ms
- hard save every 5s
- send `base_version`
- conflict creates conflict revision
- sendBeacon on page hide

- [ ] **Step 3: Implement review navigation**

Click issue evidence to select editor range.

- [ ] **Step 4: Implement diff actions**

```text
accept all
reject all
accept selected change
restore version
```

Server remains source of truth.

- [ ] **Step 5: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test \
  pnpm test -- --run
git add apps/web/src/features/writing
git commit -m "feat: add versioned writing studio"
```

---

### Task 43: Implement outline intake and export UI

**Files:**
- Create outline upload/intake wizard
- Create export feature
- Test end-to-end user flow

**Interfaces:**
- Produces the simplified ordinary-user workflow

- [ ] **Step 1: Implement upload and parse status**

States:

```text
uploading
parsing
ocr_required
needs_answers
ready_to_confirm
running
failed
```

- [ ] **Step 2: Implement clarification chat/card**

Only show missing required questions returned by API.

- [ ] **Step 3: Implement confirmation card**

Display:

- volumes
- chapters
- target words
- Writer
- Editor
- rewrite rounds
- pause policy
- cost limit

- [ ] **Step 4: Implement exports**

Formats:

```text
TXT
Markdown
DOCX
EPUB
JSON
project archive
```

- [ ] **Step 5: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test \
  pnpm test -- --run
git add apps/web/src/features/outline apps/web/src/features/exports
git commit -m "feat: add outline intake and export UI"
```

---

# Phase 10 — Health, Backup, Docker Production, and Release

### Task 44: Implement startup self-check and repair reporting

**Files:**
- Extend health service
- Create maintenance tasks
- Create settings health page
- Test: `tests/recovery/test_startup_health.py`

**Interfaces:**
- Produces maintenance mode and actionable reports

- [ ] **Step 1: Check on startup**

- PostgreSQL connection
- migration version
- Redis
- BlobStore write/read/delete
- master key
- pgvector
- incomplete uploads
- partial messages
- orphaned running workflows
- expired leases

- [ ] **Step 2: Maintenance mode**

`live` remains 200.  
`ready` returns 503 when a critical dependency fails.  
New model/workflow requests are rejected with `SERVICE_NOT_READY`.

- [ ] **Step 3: Add repair actions**

Safe actions only:

```text
requeue recoverable workflows
reindex missing embeddings
mark expired uploads failed
verify blobs
```

Destructive repair requires explicit admin request and audit log.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm recovery-test \
  pytest tests/recovery/test_startup_health.py -q
git add proseforge/application/health proseforge/workflows apps/web/src/features/settings tests/recovery
git commit -m "feat: add startup self-check and maintenance mode"
```

---

### Task 45: Implement backup, verify, and restore

**Files:**
- Create: `proseforge/application/backup/service.py`
- Create: `proseforge/cli/backup.py`
- Create: backup worker task
- Test: `tests/recovery/test_backup_restore.py`

**Interfaces:**
- Produces:

```text
proseforge backup create
proseforge backup list
proseforge backup verify <id>
proseforge backup restore <id>
```

- [ ] **Step 1: Backup contents**

- PostgreSQL dump
- blob manifest
- new/changed blobs
- prompts
- non-secret settings
- encrypted provider credentials
- checksums
- application version
- migration revision

- [ ] **Step 2: Verify before restore**

Reject:

- checksum mismatch
- missing dump
- unsupported future schema
- missing required blobs

- [ ] **Step 3: Restore into staging database first**

Run migrations and verification before switching.  
Never overwrite the only live database without a pre-restore backup.

- [ ] **Step 4: Test and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm recovery-test \
  pytest tests/recovery/test_backup_restore.py -q
git add proseforge/application/backup proseforge/cli tests/recovery
git commit -m "feat: add verified backup and restore"
```

---

### Task 46: Finalize production containers and graceful shutdown

**Files:**
- Finalize Dockerfiles, entrypoints, Nginx config, Compose healthchecks
- Test: `tests/docker/test_runtime_contract.py`

**Interfaces:**
- Produces production-ready containers

- [ ] **Step 1: Run containers as non-root**

Use fixed UID/GID and writable volume permissions.

- [ ] **Step 2: Configure Nginx SSE**

Required:

```nginx
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 3600s;
```

- [ ] **Step 3: Configure graceful shutdown**

API:

- stop accepting new work
- finish DB writes
- mark streams partial
- close resources

Worker:

- stop consuming
- checkpoint active task
- release or expire lease safely

- [ ] **Step 4: Add healthchecks to all services**

- web static response
- API readiness
- worker inspect/ping or database heartbeat
- scheduler heartbeat
- postgres
- redis

- [ ] **Step 5: Test and commit**

```bash
docker compose -f compose.yaml build
docker compose -f compose.yaml up -d
docker compose -f compose.yaml ps
docker compose -f compose.yaml exec api proseforge --version
git add docker compose.yaml compose.test.yaml tests/docker
git commit -m "build: finalize production web containers"
```

---

### Task 47: Add CI, security scanning, and visible test status

**Files:**
- Create/update `.github/workflows/ci.yml`
- Create `.github/dependabot.yml`
- Test CI config locally where possible

**Interfaces:**
- Produces required merge gates

- [ ] **Step 1: Add jobs**

```text
lint
legacy-test
api-unit
api-integration
frontend-unit
frontend-build
migration-test
provider-contract
docker-build
e2e
recovery-test
security-scan
```

- [ ] **Step 2: Add security tools**

```text
ruff
mypy
pip-audit
pnpm audit
Trivy
Gitleaks
SBOM
```

- [ ] **Step 3: Upload artifacts**

- JUnit
- coverage
- Playwright report
- migration report
- recovery report
- Trivy report
- SBOM

- [ ] **Step 4: Test workflow syntax and commit**

```bash
docker compose -f compose.yaml -f compose.test.yaml config
git diff --check
git add .github
git commit -m "ci: add complete web v1 quality gates"
```

---

### Task 48: Execute full Docker E2E and fault-injection matrix

**Files:**
- Create Playwright E2E tests
- Create recovery scripts under `tests/fault_injection/`
- Create `artifacts/WEB_V1_TEST_REPORT.md`

**Interfaces:**
- Produces evidence of end-to-end correctness

- [ ] **Step 1: E2E ordinary-user flow**

Test:

1. first admin setup
2. provider setup using mock server
3. project creation
4. outline upload
5. clarification
6. workflow confirmation
7. chapter generation stream
8. review
9. rewrite
10. commit
11. download
12. branch
13. refresh recovery

- [ ] **Step 2: API interruption**

Stop API during streaming; restart; assert PARTIAL and continuation.

- [ ] **Step 3: Worker interruption**

Stop worker during chapter 2; restart; assert no duplicate chapter 1/version.

- [ ] **Step 4: Redis data loss**

Flush Redis; restart workers; assert durable workflow requeue from PostgreSQL.

- [ ] **Step 5: Full Compose restart**

```bash
docker compose -f compose.yaml down
docker compose -f compose.yaml up -d
```

Do not use `-v`. Assert all durable data remains.

- [ ] **Step 6: Migration failure injection**

Force importer exception midway; verify source SQLite unchanged and safe retry.

- [ ] **Step 7: Backup/restore**

Create backup, remove test project, restore, compare hashes.

- [ ] **Step 8: Generate report**

Report exact commands, exit codes, test counts, image digests, failures fixed.

- [ ] **Step 9: Commit**

```bash
git add apps/web/e2e tests/fault_injection artifacts/WEB_V1_TEST_REPORT.md
git commit -m "test: validate web v1 recovery and persistence"
```

---

### Task 49: Remove compatibility debt and publish Web-first documentation

**Files:**
- Modify: `README.md`
- Create:
  - `docs/INSTALL.md`
  - `docs/MODEL_PROVIDERS.md`
  - `docs/LEGACY_MIGRATION.md`
  - `docs/BACKUP_RESTORE.md`
  - `docs/TROUBLESHOOTING.md`
  - `docs/SECURITY.md`
  - `docs/PRIVACY.md`
  - `docs/ARCHITECTURE.md`
- Modify: `VERSION`
- Modify: `CHANGELOG.md` or create it
- Delete obsolete compatibility files proven unused

**Interfaces:**
- Produces v1.0.0 release documentation

- [ ] **Step 1: Verify imports before deleting shims**

```bash
grep -R "from src" proseforge --exclude-dir=legacy_engine
grep -R "import src" proseforge --exclude-dir=legacy_engine
```

Expected: no output.

- [ ] **Step 2: Remove only unused compatibility modules**

Use test coverage and import search.  
Keep `proseforge-legacy` CLI for one migration release if legacy import still uses it.

- [ ] **Step 3: Replace README quick start**

```bash
git clone https://github.com/remacheybn408-boop/ProseForge.git
cd ProseForge
cp .env.example .env
docker compose -f compose.yaml up -d --build
```

Open:

```text
http://localhost:3000
```

- [ ] **Step 4: Document provider rule**

State:

- native adapters first
- dynamic model listing
- compatible custom endpoints marked CUSTOM
- no claim that a fixed list is eternally complete
- verification date and official docs in each adapter

- [ ] **Step 5: Set version**

`VERSION`:

```text
1.0.0
```

`pyproject.toml`:

```toml
version = "1.0.0"
```

- [ ] **Step 6: Run final completion gate**

```bash
docker compose -f compose.yaml -f compose.test.yaml build
docker compose -f compose.yaml -f compose.test.yaml run --rm legacy-test
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test
docker compose -f compose.yaml -f compose.test.yaml run --rm contract-test
docker compose -f compose.yaml -f compose.test.yaml run --rm migration-test
docker compose -f compose.yaml -f compose.test.yaml run --rm recovery-test
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test
docker compose -f compose.yaml -f compose.test.yaml up \
  --abort-on-container-exit \
  --exit-code-from e2e \
  e2e
git diff --check
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "release: prepare ProseForge Web v1.0.0"
```

---

# 4. API 契约总表

## 4.1 Health

```text
GET  /api/v1/health/live
GET  /api/v1/health/ready
GET  /api/v1/health/report
POST /api/v1/health/run
```

## 4.2 Auth

```text
POST /api/v1/auth/login
POST /api/v1/auth/logout
GET  /api/v1/auth/me
PUT  /api/v1/auth/password
```

## 4.3 Projects

```text
GET    /api/v1/projects
POST   /api/v1/projects
GET    /api/v1/projects/{project_id}
PATCH  /api/v1/projects/{project_id}
DELETE /api/v1/projects/{project_id}
POST   /api/v1/projects/{project_id}/archive
POST   /api/v1/projects/{project_id}/restore
```

## 4.4 Outlines

```text
POST /api/v1/projects/{project_id}/outlines/import
GET  /api/v1/projects/{project_id}/outlines
GET  /api/v1/outlines/{outline_id}
POST /api/v1/outlines/{outline_id}/parse
POST /api/v1/outlines/{outline_id}/confirm
POST /api/v1/outlines/{outline_id}/versions
```

## 4.5 Conversations and Branches

```text
GET    /api/v1/conversations
POST   /api/v1/conversations
GET    /api/v1/conversations/{conversation_id}
PATCH  /api/v1/conversations/{conversation_id}
DELETE /api/v1/conversations/{conversation_id}
GET    /api/v1/conversations/{conversation_id}/messages
POST   /api/v1/conversations/{conversation_id}/messages
GET    /api/v1/conversations/{conversation_id}/events
GET    /api/v1/conversations/{conversation_id}/branches
POST   /api/v1/conversations/{conversation_id}/branches
POST   /api/v1/conversations/{conversation_id}/branches/{branch_id}/activate
PATCH  /api/v1/branches/{branch_id}
POST   /api/v1/messages/{message_id}/stop
POST   /api/v1/messages/{message_id}/retry
POST   /api/v1/messages/{message_id}/continue
```

## 4.6 Files

```text
POST   /api/v1/files/uploads
PATCH  /api/v1/files/uploads/{upload_id}
POST   /api/v1/files/uploads/{upload_id}/complete
GET    /api/v1/files/{file_id}
GET    /api/v1/files/{file_id}/download
DELETE /api/v1/files/{file_id}
```

## 4.7 Providers and Models

```text
GET    /api/v1/providers
POST   /api/v1/providers
PATCH  /api/v1/providers/{provider_id}
DELETE /api/v1/providers/{provider_id}
POST   /api/v1/providers/{provider_id}/probe
POST   /api/v1/providers/{provider_id}/sync-models
GET    /api/v1/models
POST   /api/v1/model-profiles
PATCH  /api/v1/model-profiles/{profile_id}
DELETE /api/v1/model-profiles/{profile_id}
```

## 4.8 Context

```text
GET    /api/v1/projects/{project_id}/context
POST   /api/v1/projects/{project_id}/context/items
PATCH  /api/v1/context/items/{item_id}
DELETE /api/v1/context/items/{item_id}
POST   /api/v1/branches/{branch_id}/context/compile
GET    /api/v1/context/snapshots/{snapshot_id}
POST   /api/v1/context/snapshots/{snapshot_id}/validate
GET    /api/v1/context/snapshots/{snapshot_id}/download
```

## 4.9 Workflows

```text
POST /api/v1/projects/{project_id}/workflows/novel
GET  /api/v1/workflows/{workflow_id}
GET  /api/v1/workflows/{workflow_id}/events
POST /api/v1/workflows/{workflow_id}/pause
POST /api/v1/workflows/{workflow_id}/resume
POST /api/v1/workflows/{workflow_id}/cancel
POST /api/v1/workflows/{workflow_id}/retry
```

## 4.10 Chapters and Exports

```text
GET  /api/v1/projects/{project_id}/chapters
GET  /api/v1/chapters/{chapter_id}
GET  /api/v1/chapters/{chapter_id}/versions
POST /api/v1/chapters/{chapter_id}/activate-version
GET  /api/v1/chapters/{chapter_id}/diff
POST /api/v1/projects/{project_id}/exports
GET  /api/v1/exports/{export_id}
GET  /api/v1/exports/{export_id}/download
```

---

# 5. 关键状态机

## 5.1 Message

```text
PENDING -> STREAMING
PENDING -> CANCELLED
STREAMING -> COMPLETED
STREAMING -> PARTIAL
STREAMING -> FAILED
STREAMING -> CANCELLED
PARTIAL -> STREAMING through a continuation call
```

禁止：

```text
COMPLETED -> STREAMING
FAILED -> COMPLETED without a new call
```

## 5.2 Workflow

```text
CREATED
WAITING_USER
QUEUED
RUNNING
PAUSED
RETRYING
RECOVERING
COMPLETED
FAILED
CANCELLED
```

## 5.3 Chapter

```text
PLANNED
CONTEXT_READY
DRAFTING
DRAFTED
RULE_REVIEW
MODEL_REVIEW
REWRITE_REQUIRED
REWRITING
VALIDATING
COMMITTED
BLOCKED
FAILED
```

---

# 6. Provider 支持验收

首发原生 Adapter：

```text
OpenAI
Anthropic
Google Gemini
xAI
DeepSeek
Kimi/Moonshot
Alibaba DashScope
Zhipu BigModel
Volcengine Ark
Baidu Qianfan
Tencent Hunyuan
MiniMax
Mistral
Cohere
```

本地/自定义：

```text
Ollama
vLLM
OpenAI-compatible
Anthropic-compatible
```

每个 Provider 必须通过：

```text
credential probe
model list or manifest fallback
unknown future model acceptance
streaming
structured output
usage extraction
auth error
rate limit
timeout
server error
secret redaction
```

“支持最新模型”的判断标准不是代码中出现某个模型名，而是：

1. 能同步厂商模型列表。
2. 新模型无需改业务代码即可出现在 catalog。
3. capability 信息可补充。
4. 用户可手工输入官方新模型 ID。
5. 旧模型不会因同步缺失而破坏历史记录。

---

# 7. 完成定义

只有以下全部满足才允许 Codex 输出“完成”：

```text
[ ] 基线测试已保存
[ ] 外挂 Codex/Hermes/Claude Code 已删除
[ ] 新代码边界测试通过
[ ] 旧内核只有一个 Adapter 入口
[ ] PostgreSQL 主数据完成
[ ] Legacy SQLite 导入和核对完成
[ ] API Key 加密和 SSRF 防护完成
[ ] 原生 Provider 合约完成
[ ] 模型动态同步完成
[ ] Writer/Editor 双模型完成
[ ] Chat 持久化完成
[ ] 多聊天分支完成
[ ] SSE 断线恢复完成
[ ] 文件上传下载完成
[ ] Context Compiler 完成
[ ] 原文保留的上下文压缩完成
[ ] 自动大纲询问完成
[ ] 写/审/改工作流完成
[ ] Guard 与模型审稿合并完成
[ ] 工作流暂停/恢复/取消完成
[ ] Worker 重启恢复完成
[ ] 暖色 Web 完成
[ ] 正文工作台完成
[ ] Docker 生产拓扑完成
[ ] Health 自检完成
[ ] Backup/restore 完成
[ ] CI 完成
[ ] Docker 单元测试通过
[ ] Docker 集成测试通过
[ ] Provider contract 通过
[ ] Docker E2E 通过
[ ] 故障注入通过
[ ] compose down/up 数据保持通过
[ ] 文档完成
[ ] VERSION=1.0.0
```

---

# 8. 最终禁止事项

- 不得在 `proseforge/api/routes/` 中直接 import ORM model。
- 不得在 `proseforge/application/` 中 import FastAPI。
- 不得在 `proseforge/domain/` 中 import 基础设施库。
- 不得在 `proseforge/providers/` 中访问数据库。
- 不得在 `apps/web` 中保存完整 API Key。
- 不得让 Web 直接访问厂商 API。
- 不得让 Redis 成为可恢复状态的唯一来源。
- 不得在模型输出完成后才创建消息。
- 不得删除上下文压缩前的原文。
- 不得把模型摘要称为绝对无损。
- 不得硬编码永久模型清单。
- 不得自动跨厂商切换而不显示给用户。
- 不得让审稿 BLOCK 的章节成为正式版本。
- 不得让 Guard 崩溃被视为 PASS。
- 不得直接覆盖章节 canonical 内容。
- 不得迁移失败后删除旧 SQLite。
- 不得跳过 Docker 测试。
- 不得通过删除失败测试来宣布完成。
