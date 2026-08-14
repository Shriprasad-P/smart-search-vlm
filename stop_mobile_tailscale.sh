#!/bin/zsh
set -euo pipefail

PORT="${SMART_STACK_MOBILE_PORT:-8787}"
SERVICE_LABEL="com.smartstack.mobile"
AWAKE_LABEL="com.smartstack.mobile.awake"

if launchctl print "gui/${UID}/${SERVICE_LABEL}" >/dev/null 2>&1; then
    launchctl remove "${SERVICE_LABEL}"
    echo "Stopped Smart Stack Mobile background service."
fi
if launchctl print "gui/${UID}/${AWAKE_LABEL}" >/dev/null 2>&1; then
    launchctl remove "${AWAKE_LABEL}"
    echo "Restored normal Mac sleep behavior."
fi

if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
    tailscale serve --https=443 off >/dev/null 2>&1 || true
    echo "Removed the Smart Stack HTTPS endpoint from Tailscale Serve."
fi

echo "Local port ${PORT} is no longer being served by this launcher."
