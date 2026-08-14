#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
PORT="${SMART_STACK_MOBILE_PORT:-8787}"
RUNTIME_DIR="${TMPDIR:-/tmp}/smart-stack-mobile-${UID}"
LOG_FILE="${RUNTIME_DIR}/server.log"
SERVICE_LABEL="com.smartstack.mobile"
AWAKE_LABEL="com.smartstack.mobile.awake"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Smart Stack Python environment was not found at ${PYTHON_BIN}." >&2
    exit 1
fi
if ! command -v tailscale >/dev/null 2>&1; then
    echo "Tailscale is not installed or is not available on PATH." >&2
    exit 1
fi
if ! tailscale status >/dev/null 2>&1; then
    echo "Tailscale is stopped. Open Tailscale, connect this Mac, then run this again." >&2
    exit 2
fi

mkdir -p "${RUNTIME_DIR}"
launchctl remove "${SERVICE_LABEL}" >/dev/null 2>&1 || true
for _ in {1..50}; do
    if ! launchctl print "gui/${UID}/${SERVICE_LABEL}" >/dev/null 2>&1 \
        && ! lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port ${PORT} is still in use; refusing to interrupt an unknown service." >&2
    exit 1
fi
launchctl submit -l "${SERVICE_LABEL}" -o "${LOG_FILE}" -e "${LOG_FILE}" -- \
    "${PYTHON_BIN}" "${SCRIPT_DIR}/mobile_server.py" --host 127.0.0.1 --port "${PORT}"
launchctl remove "${AWAKE_LABEL}" >/dev/null 2>&1 || true
launchctl submit -l "${AWAKE_LABEL}" -- /usr/bin/caffeinate -dimsu

ready=0
for _ in {1..50}; do
    if curl --silent --fail "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        ready=1
        break
    fi
    if ! launchctl print "gui/${UID}/${SERVICE_LABEL}" >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done
if [[ "${ready}" -ne 1 ]]; then
    echo "Smart Stack Mobile did not start. See ${LOG_FILE}" >&2
    tail -n 20 "${LOG_FILE}" >&2 || true
    exit 1
fi

serve_output="$(tailscale serve --bg --yes "http://127.0.0.1:${PORT}" 2>&1)" || {
    echo "Tailscale Serve could not be enabled:" >&2
    echo "${serve_output}" >&2
    echo "The local server is still available at http://127.0.0.1:${PORT}" >&2
    exit 1
}

echo "${serve_output}"
echo ""
echo "Smart Stack Mobile is ready. Automatic sleep is disabled until the stop script runs."
echo "Local check: http://127.0.0.1:${PORT}"
echo "Background service: ${SERVICE_LABEL}"
echo "Log: ${LOG_FILE}"
