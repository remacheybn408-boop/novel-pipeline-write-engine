#!/usr/bin/env sh
set -eu
alembic upgrade head
python -m proseforge.infrastructure.database.bootstrap
exec "$@"
