#!/usr/bin/env bash
# Replace this with your controller. Contract: ./run.sh <SIM_URL> runs until the scenario finishes.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
echo "No controller yet — running the naive example against $SIM_URL" >&2
exec python examples/naive_controller.py --url "$SIM_URL"
