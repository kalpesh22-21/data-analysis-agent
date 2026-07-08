#!/usr/bin/env bash
# Dev launcher for the minimal Phase-0 UI (docs/08-ui.md, D68 Phase-0 "minimal
# UI with progress streaming"). Starts TWO processes:
#
#   1. the agent runtime on :8000. Default is the SCRIPTED runtime
#      (scripts/run_ui_runtime.py) — Layer-1 fakes for the model + MCP, so the
#      UI runs OFFLINE with no OpenAI key / no live warehouse (answers are
#      canned). Set REAL=1 to run the REAL runtime (scripts/run_ui_runtime_real.py)
#      instead — a real OpenAI model client + RealMCPClient -> live l2-mcp +
#      CouchbaseSessionStore, so the UI answers REAL questions against real
#      ClickHouse and every turn is traced to Phoenix. Both variants do REAL JWT
#      verification against the already-running l2-token container.
#   2. the UI's BFF (ui/server.py) on :3000 — serves the static page, mints
#      sessions via the real token service, and proxies/streams /turn(/resume)
#      to the runtime above.
#
# Prerequisites (this script does NOT start/stop the docker stack):
#   - Always: the l2-token service reachable at http://localhost:19000
#     (docker-compose.integration.yml's `token` service).
#   - REAL=1 additionally needs: OPENAI_API_KEY in .env, l2-mcp (:18090),
#     l2-cb (:8091/:11210), and l2-phoenix (:6006) all UP.
#
# Usage:
#   ./scripts/run_ui.sh            # scripted (offline, no key)
#   REAL=1 ./scripts/run_ui.sh     # real turns against live ClickHouse
#   (Ctrl-C stops both processes)

set -euo pipefail

cd "$(dirname "$0")/.."

if ! curl -sf http://localhost:19000/health >/dev/null 2>&1; then
  echo "WARNING: token service not reachable at http://localhost:19000 — /api/session will fail." >&2
  echo "         (docker-compose.integration.yml's 'token' service; not started by this script)" >&2
fi

if [[ "${REAL:-}" == "1" ]]; then
  echo "[run_ui] starting REAL runtime on :8000 (live model + l2-mcp + Couchbase) ..."
  uv run python scripts/run_ui_runtime_real.py &
else
  echo "[run_ui] starting scripted runtime on :8000 ..."
  uv run python scripts/run_ui_runtime.py &
fi
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
