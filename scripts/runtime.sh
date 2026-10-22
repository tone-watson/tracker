#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
case "${1:-check}" in
  sync) exec /home/gradywoodruff/.local/bin/uv sync --frozen ;;
  check)
    .venv/bin/python -m pytest
    .venv/bin/ruff check src tests
    .venv/bin/python -m compileall -q src
    ;;
  *) printf 'Usage: %s sync|check\n' "$0" >&2; exit 2 ;;
esac
