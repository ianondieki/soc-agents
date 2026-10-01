#!/usr/bin/env bash
# One-command demo stack on Linux / macOS (the PowerShell twin is scripts/run_all.ps1):
# build the UI, then serve API + UI from one port with the live agent delay on so an
# audience can watch the rail light up hop by hop.
#
#   bash scripts/run_all.sh            # http://127.0.0.1:8000
#   PORT=8080 bash scripts/run_all.sh  # another port
#   NO_BUILD=1 bash scripts/run_all.sh # skip the UI build (dist/ already fresh)
#
# Needs: python3 (3.11+) with the project installed (python -m pip install -e ".[dev]"),
# node 18+ with frontend deps installed (cd frontend && npm install).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="python3"; fi
fi

if [ "${NO_BUILD:-0}" != "1" ]; then
  echo "== Building UI =="
  (cd frontend && npm run build)
fi

# Demo posture: slow the agents down just enough to watch, keep every external channel
# mocked unless the caller's environment says otherwise.
export LIVE_AGENT_DELAY_MS="${LIVE_AGENT_DELAY_MS:-70}"
export OPERATOR_PROFILE="${OPERATOR_PROFILE:-safaricom}"

PORT="${PORT:-8000}"
echo
echo "API + UI:  http://127.0.0.1:${PORT}"
echo "Mission Control auto-runs the rain storm on an empty board; or click 'Launch heavy-rain storm (live)'."
echo "Managers: open http://127.0.0.1:${PORT}/showcase  ·  Presenters: press 'Guided demo' in the top bar."
echo

exec "$PY" -m uvicorn noc_agents.main:app --app-dir src --host 127.0.0.1 --port "$PORT"
