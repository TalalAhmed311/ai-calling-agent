#!/usr/bin/env bash
# Usage: ./dial.sh +923113302963
#        ./dial.sh   # prompts for number
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
set -a; source "$APP_DIR/.env"; set +a

TO="${1:-}"
if [ -z "$TO" ]; then
  read -rp "Phone (E.164, e.g. +923113302963): " TO
fi

curl -sS -X POST "https://${PUBLIC_HOST}/call" \
  -H "Content-Type: application/json" \
  -H "x-endpoint-secret: ${ENDPOINT_SECRET}" \
  -d "{\"to\":\"${TO}\",\"lead_name\":\"there\"}"
echo
