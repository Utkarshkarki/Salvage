#!/usr/bin/env bash
# Reclaim local demo — run the FastAPI backend and the Vite dev server together.
# Requires: uvicorn (pip install -e ".[dev]") and Node/npm.
#
# Usage:   bash scripts/dev.sh
# Stops:   Ctrl+C
#
# If you prefer two separate terminals (handy for seeing backend logs while
# editing), just run these in two shells instead:
#   Terminal 1:  uvicorn reclaim.api:app --reload
#   Terminal 2:  cd frontend && npm run dev
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo ">> Starting Reclaim backend on :8000 ..."
(cd "$ROOT" && uvicorn reclaim.api:app --host 127.0.0.1 --port 8000 --reload) &
BACKEND_PID=$!

echo ">> Starting Reclaim frontend on :5173 ..."
(cd "$ROOT/frontend" && npm run dev) &
FRONTEND_PID=$!

_trap() {
  echo
  echo ">> Shutting down (backend=$BACKEND_PID frontend=$FRONTEND_PID) ..."
  kill "$FRONTEND_PID" "$BACKEND_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap _trap INT TERM

echo ">> Open http://localhost:5173"
wait
