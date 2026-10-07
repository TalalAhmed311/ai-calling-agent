#!/usr/bin/env bash
# Hourly wrapper for recording retention (30h, skip campaigns with kept leads).
set -euo pipefail
APP_DIR="/home/ubuntu/apps/ai-calling-management-system"
exec /usr/bin/python3 "$APP_DIR/scripts/purge_recordings.py" --hours 30 >>"$APP_DIR/logs/purge_recordings.log" 2>&1
