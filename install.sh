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
