#!/usr/bin/env bash
# FormCheck launcher — starts the Python backend then Electron.
# Ctrl+C stops both processes cleanly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

# ── Cleanup: kill backend when this script exits ──────────────────────────────
cleanup() {
    if [[ -n "${BACKEND_PID:-}" ]]; then
        echo ""
        echo "[FormCheck] Stopping backend (pid $BACKEND_PID)..."
        kill "$BACKEND_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ── Kill any stale backend on port 8765 ──────────────────────────────────────
if lsof -ti:8765 > /dev/null 2>&1; then
    echo "[FormCheck] Killing stale backend on port 8765..."
    kill $(lsof -ti:8765) 2>/dev/null || true
    # Poll up to 5 s for the port to actually be released
    for i in $(seq 1 10); do
        sleep 0.5
        if ! nc -z 127.0.0.1 8765 2>/dev/null; then
            break
        fi
        if [[ $i -eq 10 ]]; then
            echo "[FormCheck] WARNING: port 8765 still in use after 5 s — trying SIGKILL"
            kill -9 $(lsof -ti:8765) 2>/dev/null || true
            sleep 1
        fi
    done
    echo "[FormCheck] Port 8765 is free."
fi

# ── Ensure HealthKit dependency is installed ──────────────────────────────────
echo "[FormCheck] Checking Python dependencies..."
pip install pyobjc-framework-HealthKit --quiet --disable-pip-version-check 2>/dev/null || true

# ── Start Python backend ──────────────────────────────────────────────────────
echo "[FormCheck] Starting Python backend..."
cd "$BACKEND_DIR"
python server.py &
BACKEND_PID=$!

# ── Wait up to 15 s for port 8765 to be ready ────────────────────────────────
echo "[FormCheck] Waiting for backend on ws://127.0.0.1:8765..."
READY=0
for i in $(seq 1 30); do
    if nc -z 127.0.0.1 8765 2>/dev/null; then
        READY=1
        break
    fi
    # Bail early if the backend process already died
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "[FormCheck] ERROR: Backend process exited unexpectedly."
        echo "[FormCheck] Check backend/logs/formcheck.log for details."
        exit 1
    fi
    sleep 0.5
done

if [[ $READY -eq 0 ]]; then
    echo "[FormCheck] ERROR: Backend did not open port 8765 within 15 s."
    echo "[FormCheck] Check backend/logs/formcheck.log for details."
    exit 1
fi

echo "[FormCheck] Backend ready."

# ── Start Electron ────────────────────────────────────────────────────────────
echo "[FormCheck] Starting Electron..."
cd "$SCRIPT_DIR"
npx electron .
