#!/usr/bin/env bash
# Start everything needed to see the 3D map on this computer, with one command:
#
#     ./dev.sh
#
# Starts the simulator service (port 8100) and the website (port 5173), opens the browser, and stops
# both when you press Ctrl+C. Also starts the backend API (port 8000) if backend/.env is set up.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
PY="$ROOT/.venv/bin/python"

[ -x "$PY" ] || { echo "Missing .venv. Run:  python3 -m venv .venv && .venv/bin/pip install -r simulation/requirements.txt"; exit 1; }
[ -d "$ROOT/simulation/data" ] || { echo "Missing simulation/data (the map files). The simulator cannot run without them."; exit 1; }
[ -d "$ROOT/frontend/node_modules" ] || { echo "Installing the website's libraries (first run only)..."; (cd "$ROOT/frontend" && npm install --no-audit --no-fund); }

if [ ! -f "$ROOT/frontend/public/sim/map/buildings.geojson" ]; then
    echo "Exporting the map for the website (first run only)..."
    (cd "$ROOT/simulation" && "$PY" -m sim.export --seed 42 --mw 7.4)
fi

pids=()
stop() { trap - EXIT INT TERM; echo; echo "Stopping..."; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; exit 0; }
trap stop EXIT INT TERM

echo "Simulator  -> http://127.0.0.1:8100"
(cd "$ROOT/simulation" && exec "$PY" -m uvicorn sim.server:app --port 8100 --log-level warning) &
pids+=($!)

if [ -f "$ROOT/backend/.env" ] && [ -x "$ROOT/backend/.venv/bin/python" ]; then
    echo "Backend    -> http://127.0.0.1:8000"
    (cd "$ROOT/backend" && exec .venv/bin/python -m uvicorn app.asgi:app --host 127.0.0.1 --port 8000 --log-level warning) &
    pids+=($!)
else
    echo "Backend    -> not started (needs backend/.env and backend/.venv; the map works without it for now)"
fi

echo "Website    -> http://localhost:5173   (Ctrl+C here stops everything)"
(cd "$ROOT/frontend" && exec npx vite --port 5173 --strictPort --open) &
pids+=($!)
wait
