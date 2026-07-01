#!/usr/bin/env bash
# Dev launcher for the minimal Phase-0 UI (docs/08-ui.md, D68 Phase-0 "minimal
# UI with progress streaming"). Starts TWO processes:
#
#   1. the scripted agent runtime (scripts/run_ui_runtime.py) on :8000 —
#      ScriptedModelClient-style fakes for the model + MCP, real JWT
#      verification against the already-running l2-token container.
#   2. the UI's BFF (ui/server.py) on :3000 — serves the static page, mints
#      sessions via the real token service, and proxies/streams /turn(/resume)
#      to the runtime above.
#
# Prerequisite: the Layer-2 token service must already be reachable at
# http://localhost:19000 (docker-compose.integration.yml's `token` service —
# this script does NOT start/stop that stack, per the "don't touch the
# running docker stack" constraint).
#
# Usage:
#   ./scripts/run_ui.sh
#   (Ctrl-C stops both processes)

set -euo pipefail

cd "$(dirname "$0")/.."

if ! curl -sf http://localhost:19000/health >/dev/null 2>&1; then
  echo "WARNING: token service not reachable at http://localhost:19000 — /api/session will fail." >&2
  echo "         (docker-compose.integration.yml's 'token' service; not started by this script)" >&2
fi

echo "[run_ui] starting scripted runtime on :8000 ..."
uv run python scripts/run_ui_runtime.py &
RUNTIME_PID=$!

cleanup() {
  echo "[run_ui] stopping runtime (pid $RUNTIME_PID) ..."
  kill "$RUNTIME_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "[run_ui] waiting for runtime health..."
for _ in $(seq 1 30); do
  curl -sf -o /dev/null http://localhost:8000/docs && break
  sleep 1
done

echo "[run_ui] starting UI BFF on :3000 ..."
uv run uvicorn ui.server:app --host 0.0.0.0 --port 3000
