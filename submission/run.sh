#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python
fi

export PYTHONPATH="$SCRIPT_DIR:$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
if "$PYTHON" -c "import submission.controller" >/dev/null 2>&1; then
  MODULE=submission.controller
else
  MODULE=controller
fi
exec "$PYTHON" -m "$MODULE" "$@"
