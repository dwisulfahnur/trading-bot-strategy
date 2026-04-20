#!/usr/bin/env bash
# Start backend and frontend dev servers

ROOT="$(cd "$(dirname "$0")" && pwd)"

# Ensure MongoDB is running via Docker Compose
if ! docker compose -f "$ROOT/docker-compose.yml" ps --services --filter status=running 2>/dev/null | grep -q mongo; then
  echo "Starting MongoDB via Docker Compose..."
  docker compose -f "$ROOT/docker-compose.yml" up -d
fi

echo "Starting FastAPI backend on http://localhost:8000 ..."
source "$ROOT/venv/bin/activate"
uvicorn backend.main:app --reload --port 8000 &
BACKEND_PID=$!

echo "Starting Vite frontend on http://localhost:5173 ..."
cd "$ROOT/frontend" && npm run dev &
FRONTEND_PID=$!

echo ""
echo "  Backend : http://localhost:8000"
echo "  Frontend: http://localhost:5173"
echo "  API docs: http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop both servers."

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" INT TERM
wait
