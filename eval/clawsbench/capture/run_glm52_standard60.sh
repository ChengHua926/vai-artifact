#!/usr/bin/env bash
set -Eeuo pipefail

RESEARCH_ROOT="/workspace"
PYTHON="/home/anon/.local/share/uv/tools/benchflow/bin/python"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$SCRIPT_DIR/run.py"

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: BenchFlow tool interpreter not found at $PYTHON" >&2
  exit 1
fi

export PYTHONPATH="$RESEARCH_ROOT/benchflow/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" "$RUNNER" --mode standard60 "$@"
