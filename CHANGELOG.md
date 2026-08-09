# Changelog

## Unreleased

- Removed the legacy desktop pipeline (2026-07-25): the `src/` CLI codebase (`proseforge-legacy` entry point), its `legacy_engine` adapter, quality analyzers, voice packs, desktop-only tests/configs/examples/docs, and the `legacy`/`rag` extras are deleted. The web application is the only supported edition; importing old desktop workspaces stays available via `proseforge migrate legacy` (unchanged, reads SQLite files directly).
- V3 agent swarm: fail-closed versioned role policies with signed snapshots, a real bounded executor (parallelism 16, lease/heartbeat, measured budgets, checkpoints), schema-validated hashed artifacts, a review swarm with persisted conflicts, an approval-bound Chief Editor that can only file V2 RevisionProposals, scoped memory with approved candidate facts, bounded auditable graph expansion, an evaluation A/B harness, /api/v3 rate limits + audit + per-user run caps (migration 0025), and a workspace Agent Swarm panel; v3 e2e specs re-enabled and passing in the container suite.
- V2 professional workspace: structured Story Bible context, Tiptap selection actions, immutable review/revision proposals, attributable model usage, durable workflow studio (definitions/runs/canvas with SSE replay), and version-pinned exports with persisted manifests.
- V2 release gate executed and green (2026-07-20, ECS Docker L2): 408 legacy + 864 api + 43 contract + 24 migration + 5 recovery + 105 web unit + 12 e2e passed, ruff clean, deterministic OpenAPI exported (111 paths); evidence in `artifacts/V2_FINAL_VALIDATION.md` and `artifacts/v2-l2-run.log`.
- Fixed v1 durable-workflow control handoff: same-owner lease takeover for resume/retry successors, RETRYING→RUNNING task start, and superseded workers yielding at chapter boundaries.
- Test infrastructure moved to a remote Linux Docker host (ECS) with mirror-patched package sources; the web test stack is reachable on port 21559.

## 1.5.0

- Added native runtime: `proseforge web` CLI entry (native profile, per-user data dirs, SQLite WAL, persisted master/jwt keys, same-origin SPA hosting).
- Added PyInstaller onedir native bundles with complete manifests (version, git SHA, Python 3.12, target, dependency hashes); Windows zip and Linux tarball builds.
- Added platform installers: Inno Setup script with per-user data preservation and backup-then-migrate upgrade flow, HKCU Run autostart scripts, deb/rpm build scripts with user-systemd unit, and signed macOS pkg scripts with LaunchAgent.
- Added real upgrade path: `proseforge upgrade` (lock, backup, migrate, doctor, rollback with integrity check, sanitized reports) and `proseforge upgrade --check` readiness probe.
- Hardened CLI: doctor infers native profile without server indicators and no longer crashes on Windows; corrupted backups fail cleanly without tracebacks.
- Fixed same-origin default for `PROSEFORGE_PUBLIC_URL` in native mode; fixed asyncpg-to-psycopg downgrade for alembic revision checks.
- CI: repaired Ruff gate, replaced compromised trivy-action with pinned digest container scan (CVE-2026-33634), fixed pnpm audit invocation, wired Playwright E2E job, and added three-OS test matrix.

## 1.1.0

- Added durable model usage records, token budgets, provider/model discovery, and bilingual Ink workspace views.
- Added production Compose overrides, secure session-cookie behavior, origin checks, and backup/restore operations guidance.

- Added authenticated React writing shell with project, outline, context and workflow flows.
- Added persistent outline/context models and migration repair for inconsistent installations.
- Added native Anthropic and Google Gemini provider adapters.
- Added Docker startup schema repair and Celery-backed application task queue.
- Added Docker-only regression, recovery, backup and security documentation.
