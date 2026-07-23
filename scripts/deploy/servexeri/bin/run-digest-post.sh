#!/usr/bin/env bash
# servexeri Monday digest post (issue #21) — posts the digest JSON that
# pinnacle BUILT and SHIPPED (run-digest.sh + CLIPSMITH_DIGEST_SHIP_TO).
#
# Contract: pinnacle is the store owner and digest builder; servexeri only
# posts. Pinnacle's producer run fires Mon 19:30 Europe/Stockholm and this
# poster fires 20:00 — but the producer's sweep can run long, so this script
# WAITS (bounded, CLIPSMITH_DIGEST_WAIT_S) for the expected week's file to be
# shipped and parseable. If it never arrives (pinnacle down since the last
# detection run), this FAILS LOUDLY (OnFailure alert) instead of silently
# posting a stale week.
#
# Env (set in ~/.config/clipsmith-deploy.env on servexeri):
#   CLIPSMITH_BOT_DIR        (default $HOME/clipsmith/bot)
#   CLIPSMITH_DIGEST_DIR     shipped digests (default $HOME/clipsmith/digests)
#   CLIPSMITH_DIGEST_WAIT_S  max seconds to wait for the shipped file
#                            (default 1200 = 20 min; must stay under the
#                            unit's TimeoutStartSec=30m)
#   CLIPSMITH_DIGEST_LIVE    1 = live post (default 0 = --dry-run; #12 gate)
#   CLIPSMITH_DIGEST_CHANNEL optional channel override for safe live testing
#   CLIPSMITH_PYTHON         must point at bin/store-python-ssh.sh for the
#                            candidate->posted flips (see unit / README)
set -u

BOT="${CLIPSMITH_BOT_DIR:-$HOME/clipsmith/bot}"
DIGESTS="${CLIPSMITH_DIGEST_DIR:-$HOME/clipsmith/digests}"
WAIT_S="${CLIPSMITH_DIGEST_WAIT_S:-1200}"

log() { echo "[run-digest-post] $*"; }

# Expected digest week: ISO week of the LAST Monday slot (not "now").
# A Persistent=true catch-up run on Tuesday for Monday's slot must post the
# PREVIOUS week, not the current one. Fix: most-recent-Monday -- not
# upcoming-or-current Monday (warden round-2 finding).
# GNU date: days_back=0 on Mon, 1 on Tue, ... gives the last Monday.
dow=$(TZ=Europe/Stockholm date +%u)                       # 1=Mon, 7=Sun
days_back=$(( (dow - 1) ))                               # 0 on Monday
WEEK=$(TZ=Europe/Stockholm date -d "-${days_back} days - 1 day" +%G-W%V)
DIGEST="$DIGESTS/$WEEK.json"

# Bounded wait for the producer (pinnacle builds/ships at 19:30; its sweep
# can run long). "Ready" = file exists AND parses as JSON, so a partially
# scp-written file is never posted. After the timeout: refuse loudly.
ready() { [ -f "$DIGEST" ] && node -e "JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'))" "$DIGEST" 2>/dev/null; }
deadline=$(( $(date +%s) + WAIT_S ))
until ready; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        log "ERROR: expected digest $DIGEST not shipped from pinnacle within ${WAIT_S}s — refusing to post a stale week"
        exit 1
    fi
    log "waiting for $DIGEST (producer still running? retry in 30s, deadline in $(( deadline - $(date +%s) ))s)"
    sleep 30
done

cd "$BOT" || { log "ERROR: bot dir missing at $BOT"; exit 1; }
if [ "${CLIPSMITH_DIGEST_LIVE:-0}" = "1" ]; then
    log "posting $WEEK LIVE"
    exec node src/post-digest.mjs --digest "$DIGEST" \
        ${CLIPSMITH_DIGEST_CHANNEL:+--channel "$CLIPSMITH_DIGEST_CHANNEL"}
else
    log "posting $WEEK in DRY-RUN (flip CLIPSMITH_DIGEST_LIVE=1 after #12)"
    exec node src/post-digest.mjs --digest "$DIGEST" --dry-run
fi
