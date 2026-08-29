#!/usr/bin/env bash
# IncidentPilot — one-command launcher.
# Starts the Holmes port-forward + the ops console, cleans both up on Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8010}"
HOLMES_PORT="${HOLMES_PORT:-10001}"
export HOLMES_URL="${HOLMES_URL:-http://localhost:$HOLMES_PORT}"

echo "==> Checking prerequisites"
command -v kubectl >/dev/null || { echo "kubectl not found"; exit 1; }
kubectl get svc robusta-holmes -n default >/dev/null 2>&1 \
  || { echo "robusta-holmes service not found — is Robusta installed? (helm install robusta ...)"; exit 1; }

if [ ! -x .venv/bin/uvicorn ]; then
  echo "==> Creating venv + installing dependencies (first run only)"
  python3 -m venv .venv
  .venv/bin/pip install -q -r console/requirements.txt
fi

echo "==> Stopping any previous instance"
pkill -f "uvicorn console.main:app" 2>/dev/null || true
pkill -f "port-forward.*robusta-holmes" 2>/dev/null || true
sleep 1

echo "==> Port-forwarding HolmesGPT on :$HOLMES_PORT"
kubectl port-forward -n default svc/robusta-holmes "$HOLMES_PORT:80" >/dev/null 2>&1 &
PF_PID=$!
trap 'kill "$PF_PID" 2>/dev/null || true' EXIT

echo "==> Console starting on http://localhost:$PORT"
exec .venv/bin/uvicorn console.main:app --host 0.0.0.0 --port "$PORT"
