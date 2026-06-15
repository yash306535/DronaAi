#!/usr/bin/env bash
# DRONA AI — one-command MVP launcher.
# Boots the backend (with demo data) and the frontend, then prints the URLs.
#
# Usage:
#   ./run-mvp.sh          # set up + seed + run backend & frontend
#   ./run-mvp.sh --reseed # also wipe & reseed the demo database
#
# Stop everything with Ctrl+C.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
RESEED="${1:-}"

echo "==> DRONA AI MVP launcher"

# --- Free the ports we need (8000 backend, 5173 frontend) -------------------
free_port() {
  local port="$1"
  local pids
  pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "==> Freeing port $port (killing: $pids)"
    kill $pids 2>/dev/null || true
    sleep 1
  fi
}
free_port 8000
free_port 5173

# --- Backend setup ----------------------------------------------------------
cd "$BACKEND"

if [ ! -x ".venv/bin/python" ]; then
  echo "==> Creating Python 3.12 virtualenv"
  python3.12 -m venv .venv
fi

echo "==> Installing backend dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f ".env" ]; then
  echo "==> No .env found — copying from .env.example (edit it to add your OPENAI_API_KEY)"
  cp .env.example .env
fi

if [ "$RESEED" = "--reseed" ] || [ ! -f "drona.db" ]; then
  echo "==> Seeding demo data"
  .venv/bin/python -m app.seed
fi

echo "==> Starting backend on http://localhost:8000"
.venv/bin/uvicorn app.main:app --reload --port 8000 &
BACKEND_PID=$!

# --- Frontend setup ---------------------------------------------------------
cd "$FRONTEND"

if [ ! -d "node_modules" ]; then
  echo "==> Installing frontend dependencies"
  npm install
fi

echo "==> Starting frontend on http://localhost:5173"
npm run dev &
FRONTEND_PID=$!

# --- Done -------------------------------------------------------------------
cat <<EOF

============================================================
  DRONA AI MVP is starting up
------------------------------------------------------------
  Frontend : http://localhost:5173
  Backend  : http://localhost:8000  (API docs: /docs)

  Demo logins (Quick demo access buttons on the login page):
    Admin       admin@drona.ai        / AdminPass123!
    Invigilator invigilator@drona.ai  / InvigilatorPass123!
    Student     student4@drona.ai     / StudentPass123!
============================================================

  Press Ctrl+C to stop both servers.
EOF

# Clean up both child processes on exit.
trap 'echo; echo "==> Stopping servers"; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true' INT TERM
wait
