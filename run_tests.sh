#!/usr/bin/env bash
set -e

# Unified Python detection: prefer venv, fall back to system
PYTHON=${PYTHON:-python3}
if [ -d ".venv" ]; then
  . .venv/bin/activate 2>/dev/null || true
  PYTHON=python
fi

$PYTHON -m pytest tests/ -q "$@"
