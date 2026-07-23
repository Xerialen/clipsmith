#!/usr/bin/env bash
# clipsmith detection run (issue #21) — fired by clipsmith-detect.timer
# (Tue/Thu/Sat 23:33 Europe/Stockholm) or manually:
#   sudo systemctl start clipsmith-detect.service
#
# Steps:
#   1. ensure mvd-api answers /healthz on :8080 (start clipsmith-mvd-api.service
#      only if nothing healthy is listening — the MCP launcher may own the port)
#   2. python -m clipsmith.watcher           (new Turso 4on4 demos -> download +
#                                             state/pending-detection.jsonl)
#   3. run_detectors.py                      (consume pending-detection.jsonl:
#                                             the four Phase-1 detectors per sha,
#                                             per-detector ledger so a partial
#                                             failure never double-emits; then a
#                                             hub_links resolution pass)
#
# Exit: non-zero if the watcher or any detector failed (OnFailure= hook fires);
# hub-link "not on hub yet" (exit 3) is informational and retried next run.
#
# Env knobs (set in ~/.config/clipsmith-deploy.env, all optional):
#   CLIPSMITH_DEPLOY_DIR  deploy clone        (default $HOME/clipsmith)
#   CLIPSMITH_STATE_DIR   canonical state dir (default $HOME/.local/share/clipsmith)
#   CLIPSMITH_DB          candidate store     (default $CLIPSMITH_STATE_DIR/candidates.db)
#   CLIPSMITH_API_URL     mvd-api             (default http://127.0.0.1:8080)
#   CLIPSMITH_LOCK_FILE   shared detect/digest lock
#                         (default /tmp/clipsmith-${USER:-user}.lock)
set -u

REPO="${CLIPSMITH_DEPLOY_DIR:-$HOME/clipsmith}"
STATE="${CLIPSMITH_STATE_DIR:-$HOME/.local/share/clipsmith}"
DB="${CLIPSMITH_DB:-$STATE/candidates.db}"
API="${CLIPSMITH_API_URL:-http://127.0.0.1:8080}"
LOCK_FILE="${CLIPSMITH_LOCK_FILE:-/tmp/clipsmith-${USER:-user}.lock}"

log() { echo "[run-detect] $*"; }

# --- single-runner lock (detect and digest both write the store; Persistent
# --- catch-up after a boot can fire both timers at once). Reentrant via env
# --- guard: run-digest.sh holds the lock and then calls this script.
if [ "${CLIPSMITH_LOCK_HELD:-0}" != "1" ]; then
    exec 9>"$LOCK_FILE"
    if ! flock -w 1800 9; then
        log "ERROR: could not acquire $LOCK_FILE within 30 min"
        exit 1
    fi
    export CLIPSMITH_LOCK_HELD=1
fi

# --- 1. mvd-api availability -------------------------------------------------
healthy() { curl -fsS -m 3 "$API/healthz" >/dev/null 2>&1; }

if healthy; then
    log "mvd-api healthy at $API"
else
    log "mvd-api not answering at $API — starting clipsmith-mvd-api.service"
    sudo -n systemctl start clipsmith-mvd-api.service 2>/dev/null \
        || systemctl start clipsmith-mvd-api.service
    for _ in $(seq 1 30); do
        healthy && break
        sleep 1
    done
    if ! healthy; then
        log "ERROR: mvd-api still unhealthy after 30 s (journalctl -u clipsmith-mvd-api)"
        exit 1
    fi
    log "mvd-api up"
fi

cd "$REPO" || { log "ERROR: deploy clone missing at $REPO (run install.sh)"; exit 1; }
rc=0

# --- 2. watcher ---------------------------------------------------------------
# Exit 1 = partial failure (watermark frozen before the first failed game and
# retried next run). Detection still proceeds over what IS pending.
log "watcher: polling Turso for new 4on4 demos (state: $STATE)"
if ! python3 -m clipsmith.watcher --state-dir "$STATE"; then
    log "WARN: watcher reported failures (watermark frozen; will retry next run)"
    rc=1
fi

# --- 3. detectors + hub links ---------------------------------------------------
log "detectors: consuming pending-detection.jsonl"
if ! python3 "$REPO/scripts/deploy/pinnacle/run_detectors.py" \
        --repo "$REPO" --state-dir "$STATE" --db "$DB" --api-url "$API"; then
    rc=1
fi

log "detection run finished rc=$rc"
exit $rc
