#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_ROOT="${HERMES_ROUTER_WORK_DIR:-$HOME/codex/hermes-router/work}"
mkdir -p "$WORK_ROOT"
cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1
export COVERAGE_FILE="$WORK_ROOT/.coverage"
exec .venv/bin/python -m pytest -q --tb=short \
  -o "cache_dir=$WORK_ROOT/pytest-cache" --basetemp "$WORK_ROOT/pytest-tmp" \
  --cov=hermes_router --cov-report=term-missing \
  --cov-report="json:$WORK_ROOT/coverage.json" --junitxml="$WORK_ROOT/tests.xml" "$@"
