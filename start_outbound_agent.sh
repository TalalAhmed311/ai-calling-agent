#!/usr/bin/env bash
# Start Cloudflare quick tunnel + outbound Telnyx/Deepgram agent.
# PUBLIC_HOST is updated before uvicorn starts so webhook/media URLs match.
set -euo pipefail

APP_DIR="/home/ubuntu/apps/ai-calling-management-system"
LOG_DIR="$APP_DIR/logs"
CF_LOG="$LOG_DIR/cloudflared-outbound.log"
AGENT_LOG="$LOG_DIR/outbound-agent.log"
ENV_FILE="$APP_DIR/.env"
PORT=8081
VENV="$APP_DIR/venv"

mkdir -p "$LOG_DIR"
: > "$CF_LOG"
: > "$AGENT_LOG"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Free stale listeners if any
fuser -k "${PORT}/tcp" 2>/dev/null || true
pkill -f "cloudflared tunnel --url http://127.0.0.1:${PORT}" 2>/dev/null || true
sleep 1

/usr/local/bin/cloudflared tunnel --url "http://127.0.0.1:${PORT}" >>"$CF_LOG" 2>&1 &
CF_PID=$!

TUNNEL_URL=""
for _ in $(seq 1 60); do
  if TUNNEL_URL=$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$CF_LOG" | tail -1); then
    if [ -n "$TUNNEL_URL" ]; then
      break
    fi
  fi
  sleep 1
done

if [ -z "$TUNNEL_URL" ]; then
  echo "Failed to obtain Cloudflare tunnel URL" >&2
  kill "$CF_PID" 2>/dev/null || true
  exit 1
fi

PUBLIC_HOST="${TUNNEL_URL#https://}"
echo "Tunnel ready: $TUNNEL_URL (PUBLIC_HOST=$PUBLIC_HOST)"

if grep -q '^PUBLIC_HOST=' "$ENV_FILE"; then
  sed -i "s|^PUBLIC_HOST=.*|PUBLIC_HOST=${PUBLIC_HOST}|" "$ENV_FILE"
else
  echo "PUBLIC_HOST=${PUBLIC_HOST}" >>"$ENV_FILE"
fi

export PUBLIC_HOST

cd "$APP_DIR"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
# Re-load env so uvicorn child sees updated PUBLIC_HOST
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
export PYTHONUNBUFFERED=1

"$VENV/bin/uvicorn" calling_agent.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --log-level info \
  >>"$AGENT_LOG" 2>&1 &
AGENT_PID=$!

cleanup() {
  kill "$AGENT_PID" "$CF_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait until health responds
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null; then
    echo "Outbound agent healthy on :${PORT}"
    echo "Public: https://${PUBLIC_HOST}/health"
    echo "Webhook: https://${PUBLIC_HOST}/telnyx/events"
    echo "Media WSS: wss://${PUBLIC_HOST}/telnyx/media"
    break
  fi
  sleep 1
done

wait "$AGENT_PID"
