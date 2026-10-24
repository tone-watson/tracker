#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
case "${1:-check}" in
  sync)
    /home/gradywoodruff/.local/bin/uv sync --frozen
    PATH="/home/gradywoodruff/.nvm/versions/node/v22.23.4/bin:$PATH" npm ci --ignore-scripts
    ;;
  check)
    .venv/bin/python -m pytest
    .venv/bin/ruff check src tests scripts/setup_web.py
    .venv/bin/python -m compileall -q src scripts/setup_web.py
    PATH="/home/gradywoodruff/.nvm/versions/node/v22.23.4/bin:$PATH" npm test
    /home/gradywoodruff/.nvm/versions/node/v22.23.4/bin/node --check web/public/app.js
    ;;
  *) printf 'Usage: %s sync|check\n' "$0" >&2; exit 2 ;;
esac
