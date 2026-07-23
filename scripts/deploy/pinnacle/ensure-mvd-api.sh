#!/usr/bin/env bash
# Ensure an mvd-api answers on :8080 before a clipsmith run (issue #21).
# Invoked as ExecStartPre=+... (full privileges) from clipsmith-detect.service
# and clipsmith-digest.service, so it may systemctl-start the on-demand
# clipsmith-mvd-api.service without sudo/polkit friction.
#
# If something healthy already listens (e.g. the ad-hoc instance started by
# ~/mvd-mcp-bundle/run-mcp.sh), this is a no-op — never fight over the port.
set -u

API="${CLIPSMITH_API_URL:-http://127.0.0.1:8080}"
healthy() { curl -fsS -m 3 "$API/healthz" >/dev/null 2>&1; }

if healthy; then
    echo "[ensure-mvd-api] healthy at $API"
    exit 0
fi

echo "[ensure-mvd-api] starting clipsmith-mvd-api.service"
systemctl start clipsmith-mvd-api.service

for _ in $(seq 1 30); do
    healthy && { echo "[ensure-mvd-api] up"; exit 0; }
    sleep 1
done

echo "[ensure-mvd-api] ERROR: mvd-api still unhealthy after 30 s" >&2
exit 1
