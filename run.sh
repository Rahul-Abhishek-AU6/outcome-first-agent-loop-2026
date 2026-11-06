#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> runs until the scenario finishes.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
STATE_DIR="${CONTROLLER_STATE_DIR:-.controller-state}"
mkdir -p "$STATE_DIR"
exec python controller.py --url "$SIM_URL" --state "$STATE_DIR/controller.sqlite3"
