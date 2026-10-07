#!/usr/bin/env bash
# Start a Cloudflare quick tunnel and keep the assistant serverUrl in sync.
set -euo pipefail

APP_DIR="/home/ubuntu/apps/ai-calling-management-system"
LOG_FILE="$APP_DIR/logs/cloudflared.log"
ENV_FILE="$APP_DIR/.env"

mkdir -p "$APP_DIR/logs"
: > "$LOG_FILE"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

/usr/local/bin/cloudflared tunnel --url http://127.0.0.1:5000 >>"$LOG_FILE" 2>&1 &
CF_PID=$!

# Wait for quick-tunnel public URL
TUNNEL_URL=""
for _ in $(seq 1 60); do
  if TUNNEL_URL=$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$LOG_FILE" | tail -1); then
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

WEBHOOK_URL="${TUNNEL_URL}/vapi-webhook"
echo "Tunnel ready: $WEBHOOK_URL"

if [ -n "${VAPI_API_KEY:-}" ] && [ -n "${VAPI_ASSISTANT_ID:-}" ]; then
  curl -sS -X PATCH "https://api.vapi.ai/assistant/${VAPI_ASSISTANT_ID}" \
    -H "Authorization: Bearer ${VAPI_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"serverUrl\":\"${WEBHOOK_URL}\",\"serverMessages\":[\"end-of-call-report\"]}" \
    >/dev/null
  echo "Assistant serverUrl updated"
fi

if grep -q '^VAPI_SERVER_URL=' "$ENV_FILE"; then
  sed -i "s|^VAPI_SERVER_URL=.*|VAPI_SERVER_URL=${WEBHOOK_URL}|" "$ENV_FILE"
else
  echo "VAPI_SERVER_URL=${WEBHOOK_URL}" >>"$ENV_FILE"
fi

wait "$CF_PID"
