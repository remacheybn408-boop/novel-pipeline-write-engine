# Docker-only testing

All ProseForge builds, tests, migrations, and browser runs execute inside Docker. The host only needs Docker Desktop, Compose, and Git; do not run the Python or Node toolchains directly on the host.

## Production stack

```bash
docker compose -f compose.yaml up -d
docker compose -f compose.yaml ps
```

Wait for PostgreSQL, Redis, API, worker, scheduler, and web to report `healthy`.

## Isolated test stack

`compose.test.yaml` overrides PostgreSQL and Redis with `postgres-test-data` and `redis-test-data`. Never use `down -v` on the dev/prod stack: its named volumes contain durable user data. The disposable test stack is the exception — the test execution policy tears it down with `down -v` between batches, which wipes only the `*-test-data` volumes and the disposable Node/pnpm cache volumes (`web-node-modules`, `web-pnpm-store`).

Run the main suites with:

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm api-test
docker compose -f compose.yaml -f compose.test.yaml run --rm contract-test
docker compose -f compose.yaml -f compose.test.yaml run --rm migration-test
docker compose -f compose.yaml -f compose.test.yaml run --rm recovery-test
docker compose -f compose.yaml -f compose.test.yaml run --rm web-test
```

## Browser E2E after a test-volume switch

When the combined Compose project replaces the database volume, force-recreate the API and dependent services so their startup migration/bootstrap runs against the same database:

```bash
docker compose -f compose.yaml -f compose.test.yaml up -d --force-recreate api worker scheduler web
docker compose -f compose.yaml -f compose.test.yaml run --rm e2e
```

This prevents a running API from retaining a connection to the previous database volume. The E2E stack includes a local mock provider and verifies setup/login, project creation, outline clarification, chapter workflow, version save, encrypted provider setup, and worker-backed chat streaming.

## Return to production

```bash
docker compose -f compose.yaml up -d
docker compose -f compose.yaml exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready').read().decode())"
```

Do not run `docker compose down -v`; ordinary `down` preserves PostgreSQL, Redis, BlobStore, and backup volumes.
