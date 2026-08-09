# Current Test Baseline

- Revision: `bbf2cf82c0b246a5be3be7576b977586bfbcfb58` (`master`)
- This is a **structural baseline**: it describes the current test layout and the
  Docker test services that exercise it. It does not record pass/fail counts;
  historical counts (e.g. the v1.1 run: 408 legacy + 531 api at revision
  `f6183d1`) are preserved only in `CHANGELOG.md`.

## Test layout

- `tests/` holds the entire pytest suite: ~200 test files, ~970 collected cases
  (collector count, not a pass count).
- Categories: `unit`, `api`, `integration`, `contract`, `migration`,
  `recovery`, `agents`, `architecture`, `cli`, `database`, `docker`,
  `fault_injection`, `operations`, `packaging`, `runtime`, `security`, `tasks`.
- Browser E2E specs live in `tests/e2e/` (Playwright, run via `apps/web`).
- The legacy desktop test suite was deleted on 2026-07-25 together with the
  legacy `src/` codebase (see `CHANGELOG.md`); the `legacy-test` Docker service
  no longer exists.

## Docker test services (`compose.test.yaml`)

- `api-test` — full pytest run of `tests/` (PostgreSQL + Redis)
- `contract-test` — `tests/contract`
- `migration-test` — `tests/migration` + `tests/integration/database`
- `recovery-test` — `tests/recovery`
- `web-test` — web typecheck, unit tests, and Vite build
- `e2e` — Playwright specs against the running stack with `provider-mock`

See `docs/DOCKER_TESTING.md` for the exact commands. JUnit artifacts are
written under `artifacts/`.
