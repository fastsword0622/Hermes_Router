#!/usr/bin/env bash
# Hermes Router installer: project-local .venv only. No Hermes core/provider changes.
set -euo pipefail
umask 077
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/codex/hermes-router/cache/uv}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [--install-skill] [--help]
  (no flag)      sync project-local .venv via uv sync --frozen --extra dev
  --install-skill  copy skills/dev-router -> ~/.hermes/skills/dev-router
                   only when destination is absent or byte-identical;
                   existing customised files are never overwritten.
EOF
}

INSTALL_SKILL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --install-skill) INSTALL_SKILL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

echo "==> uv sync --frozen --extra dev (project-local .venv)"
uv sync --frozen --extra dev

echo "==> verify entrypoints"
.venv/bin/python -c "import hermes_router, sys; print('python', sys.version.split()[0], 'hermes_router', hermes_router.__file__)"
.venv/bin/hermes-router --providers >/dev/null

if [ "$INSTALL_SKILL" -eq 1 ]; then
  .venv/bin/python - "$ROOT" "$HOME/.local/bin/dev-router" <<'PY'
from pathlib import Path
import shlex
import sys
root, destination = Path(sys.argv[1]), Path(sys.argv[2])
text = '#!/bin/sh\n# Managed entrypoint for Hermes Development Router.\nexec ' + shlex.quote(str(root / '.venv/bin/dev-router')) + ' "$@"\n'
if destination.is_symlink() or (destination.exists() and destination.read_text() != text):
    raise SystemExit('error: existing dev-router entrypoint preserved; install manually')
destination.parent.mkdir(parents=True, exist_ok=True)
if not destination.exists():
    with destination.open('x') as handle:
        handle.write(text)
    destination.chmod(0o755)
PY
  SRC="$ROOT/skills/dev-router"
  DST="$HOME/.hermes/skills/dev-router"
  if [ -L "$DST" ]; then
    echo "error: existing skill symlink is preserved; install manually" >&2
    exit 1
  fi
  if [ ! -d "$SRC" ]; then
    echo "error: skill source missing: $SRC" >&2
    exit 1
  fi
  mkdir -p "$DST"
  status=0
  while IFS= read -r rel; do
    mkdir -p "$DST/$(dirname "$rel")"
    if [ ! -e "$DST/$rel" ]; then
      cp -p "$SRC/$rel" "$DST/$rel"
      echo "installed: $rel"
    elif cmp -s "$SRC/$rel" "$DST/$rel"; then
      echo "identical: $rel"
    else
      echo "skipped (local modification kept): $rel" >&2
      status=1
    fi
  done < <(cd "$SRC" && find . -type f -print | sed 's|^\./||')
  if [ "$status" -ne 0 ]; then
    echo "note: some skill files were kept as-is; merge manually if desired." >&2
    exit 1
  fi
fi

echo "install ok"
