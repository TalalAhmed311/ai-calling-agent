#!/usr/bin/env bash
# Streamlit campaign UI for the outbound agent.
set -euo pipefail

APP_DIR="/home/ubuntu/apps/ai-calling-management-system"
VENV="$APP_DIR/venv"
PORT="${STREAMLIT_PORT:-8503}"

cd "$APP_DIR"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
set -a
# shellcheck disable=SC1091
source "$APP_DIR/.env"
set +a
export AGENT_URL="${AGENT_URL:-http://127.0.0.1:8081}"
export PYTHONUNBUFFERED=1

exec "$VENV/bin/streamlit" run "$APP_DIR/streamlit_app.py" \
  --server.port "$PORT" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.enableCORS false \
  --server.enableXsrfProtection false \
  --browser.gatherUsageStats false
