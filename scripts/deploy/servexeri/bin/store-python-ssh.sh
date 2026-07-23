#!/usr/bin/env bash
# Remote candidate-store access for the servexeri bot (issue #21 host split).
#
# bot/src/store.mjs shells out to `$CLIPSMITH_PYTHON -m clipsmith.store …`.
# Pointing CLIPSMITH_PYTHON at this wrapper makes every store call execute on
# PINNACLE (the store owner) over SSH — the SQLite db is never copied or
# multi-written; pinnacle stays the single writer. Zero bot-code changes.
#
# When pinnacle/WSL is down the call fails fast; post-digest.mjs already
# treats flip failures as logged + non-fatal (exit 1 -> OnFailure alert), and
# the flips are recoverable later on pinnacle:
#   python -m clipsmith.digest mark-posted --week <week>
#
# Env (set in ~/.config/clipsmith-deploy.env on servexeri):
#   CLIPSMITH_PINNACLE_SSH  required ssh target for the store host
#   CLIPSMITH_PINNACLE_REPO deploy clone on store host (default clipsmith)
#   CLIPSMITH_PINNACLE_DB   store path on store host (default clipsmith/state/candidates.db)
#   CLIPSMITH_SSH_TIMEOUT_S whole-command ceiling (default 120) — covers hangs
#                           AFTER the connection is up (ConnectTimeout only
#                           guards establishment); keepalives kill dead links.
set -u

TARGET="${CLIPSMITH_PINNACLE_SSH:-}"
REPO="${CLIPSMITH_PINNACLE_REPO:-clipsmith}"
DB="${CLIPSMITH_PINNACLE_DB:-clipsmith/state/candidates.db}"
TIMEOUT_S="${CLIPSMITH_SSH_TIMEOUT_S:-120}"

log() { echo "[store-python-ssh] $*" >&2; }

if [ -z "$TARGET" ]; then
    log "ERROR: CLIPSMITH_PINNACLE_SSH is required"
    exit 64
fi

# Validate the ssh target: never let a value starting with '-' be parsed as
# an ssh option, and require a plain [user@]host (alnum/dot/dash, optional
# user part) — anything else is a misconfiguration, not a destination.
case "$TARGET" in
    -*) log "ERROR: CLIPSMITH_PINNACLE_SSH starts with '-' (would parse as an ssh option): $TARGET"; exit 64 ;;
esac
if ! printf '%s' "$TARGET" | grep -Eq '^([A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9.-]*$'; then
    log "ERROR: CLIPSMITH_PINNACLE_SSH is not a plain [user@]host: $TARGET"
    exit 64
fi

# %q-quote each argument so player names / JSON survive the remote shell.
printf -v REMOTE_ARGS '%q ' "$@"
timeout --kill-after=10 "$TIMEOUT_S" \
    ssh -o BatchMode=yes -o ConnectTimeout=10 \
        -o ServerAliveInterval=5 -o ServerAliveCountMax=3 \
        -- "$TARGET" \
        "cd $(printf '%q' "$REPO") && CLIPSMITH_DB=$(printf '%q' "$DB") python3 $REMOTE_ARGS"
rc=$?
if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    log "ERROR: remote store call to $TARGET exceeded ${TIMEOUT_S}s (timeout rc=$rc): python3 $REMOTE_ARGS"
elif [ "$rc" -ne 0 ]; then
    log "ERROR: remote store call to $TARGET failed rc=$rc: python3 $REMOTE_ARGS"
fi
exit "$rc"
