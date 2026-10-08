#!/bin/bash
# Claude Code sessions run the project on the same Python as production (.python-version, 3.13.5): a .venv built with uv, with the
# test dependencies, put first on PATH. Safe to run again: it only rebuilds a venv that is on the wrong Python.
set -euo pipefail
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"
want="$(tr -d '[:space:]' < .python-version)"
if ! .venv/bin/python -c "import sys; sys.exit(sys.version_info[:2] != tuple(map(int, '${want}'.split('.')[:2])))" 2>/dev/null; then
  rm -rf .venv
  uv venv --python "$want" .venv 2>/dev/null || uv venv --python "${want%.*}" .venv   # the exact patch when uv can fetch it, else the installed 3.13
fi
uv pip install --quiet --python .venv/bin/python -r requirements-dev.txt
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export VIRTUAL_ENV=\"$PWD/.venv\"" >> "$CLAUDE_ENV_FILE"
  echo "export PATH=\"$PWD/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
  # Draw charts as CI does: ignore a system matplotlibrc (this container's turns text hinting off), or the reference images drift.
  echo "backend: agg" > .venv/matplotlibrc
  echo "export MATPLOTLIBRC=\"$PWD/.venv/matplotlibrc\"" >> "$CLAUDE_ENV_FILE"
fi
