#!/usr/bin/env bash
# Start the router in the foreground using the project-local .venv.
# Any arguments are passed straight to python -m hermes_router.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "error: $PY missing; run scripts/install.sh first" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p "${HERMES_ROUTER_DATA_DIR:-$HOME/codex/hermes-router/data}"

exec "$PY" -m hermes_router "$@"
