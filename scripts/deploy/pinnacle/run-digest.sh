#!/usr/bin/env bash
# clipsmith Monday digest run (issue #21) — fired by clipsmith-digest.timer
# (Mon 20:00 Europe/Stockholm) or manually:
#   sudo systemctl start clipsmith-digest.service
#
# Steps:
#   1. best-effort pre-digest detection sweep (run-detect.sh). Why: detection
#      is Tue/Thu/Sat, so Sunday's games would otherwise be detected only on
#      Tuesday — AFTER their week's Monday digest — and never get posted. The
#      sweep closes that gap for everything Turso has synced by Monday 20:00.
#      A sweep failure is logged but never blocks the digest (post what we have).
#   2. python -m clipsmith.digest build      (state/digests/<week>.json)
#   3. optional ship of the digest JSON      (CLIPSMITH_DIGEST_SHIP_TO, for the
#                                             servexeri bot host)
#   4. node bot/src/post-digest.mjs          (--dry-run until the owner flips
#                                             CLIPSMITH_DIGEST_LIVE=1 after #12;
#                                             SKIPPED in CLIPSMITH_DIGEST_MODE=build)
#
# Producer/poster split (servexeri switchover, see ../servexeri/README.md):
# this script is the ONLY producer of the digest JSON — it must keep running
# on pinnacle even after the post moves to servexeri. At switchover the owner
# sets CLIPSMITH_DIGEST_MODE=build (sweep + build + ship, no local post); the
# timer stays ENABLED. Do not disable clipsmith-digest.timer — that would
# starve servexeri's poster of its input.
#
# Env knobs (set in ~/.config/clipsmith-deploy.env, all optional):
#   CLIPSMITH_DEPLOY_DIR     deploy clone     (default $HOME/clipsmith)
#   CLIPSMITH_STATE_DIR      canonical state  (default $HOME/.local/share/clipsmith)
#   CLIPSMITH_DB             candidate store  (default $CLIPSMITH_STATE_DIR/candidates.db)
#   CLIPSMITH_DIGEST_MODE    full (default) = build + ship + local post;
#                            build = producer-only (servexeri posts; switchover)
#   CLIPSMITH_DIGEST_LIVE    1 = post live to Discord (default 0 = --dry-run; #12 gate)
#   CLIPSMITH_DIGEST_CHANNEL channel-id override for a safe first live test
#   CLIPSMITH_DIGEST_SHIP_TO scp target, e.g. <user>@<bot-host>:clipsmith/digests/
#   CLIPSMITH_LOCK_FILE      shared digest/detect lock
#                             (default /tmp/clipsmith-${USER:-user}.lock)
set -u

REPO="${CLIPSMITH_DEPLOY_DIR:-$HOME/clipsmith}"
STATE="${CLIPSMITH_STATE_DIR:-$HOME/.local/share/clipsmith}"
DB="${CLIPSMITH_DB:-$STATE/candidates.db}"
LOCK_FILE="${CLIPSMITH_LOCK_FILE:-/tmp/clipsmith-${USER:-user}.lock}"

log() { echo "[run-digest] $*"; }

# Serialize against clipsmith-detect (both can fire together on Persistent
# catch-up after a boot). run-detect.sh sees CLIPSMITH_LOCK_HELD and skips
# re-locking.
exec 9>"$LOCK_FILE"
if ! flock -w 1800 9; then
    log "ERROR: could not acquire $LOCK_FILE within 30 min"
    exit 1
fi
export CLIPSMITH_LOCK_HELD=1

cd "$REPO" || { log "ERROR: deploy clone missing at $REPO (run install.sh)"; exit 1; }

# --- 1. pre-digest detection sweep (best effort) ------------------------------
# Gated on the detect timer being enabled: install.sh refuses to enable it
# while emit_candidate lacks #39 natural-key idempotency, and this sweep must
# not bypass that gate (it runs the same detectors). Also respects a manual
# owner pause of the detection schedule.
if [ "$(systemctl is-enabled clipsmith-detect.timer 2>/dev/null)" = "enabled" ]; then
    log "pre-digest detection sweep (Sunday-games gap)"
    if ! "$REPO/scripts/deploy/pinnacle/run-detect.sh"; then
        log "WARN: pre-digest detection sweep failed — posting digest from what the store has"
    fi
else
    log "WARN: clipsmith-detect.timer not enabled (idempotency gate or owner pause) — skipping pre-digest sweep, posting digest from what the store has"
fi

# --- 2. build the week's digest -----------------------------------------------
# Derive the target week from the most recent Monday slot, not wall-clock "now".
# A Persistent=true catch-up run on Tuesday for Monday's slot must build the
# previous ISO week, not the current one (warden finding — run-digest round-2).
# Logic: "most recent Monday" = today if weekday==1, else today minus N days.
WEEK="$(python3 -c '
import datetime as _dt, sys
today = _dt.date.today()
# weekday(): 0=Mon,...,6=Sun; days_since_monday=0 on Monday
days_back = (today.weekday()) % 7  # 0 on Monday, 1 on Tuesday, ...
last_monday = today - _dt.timedelta(days=days_back)
# ISO week of the previous finished week (last_monday - 1 day, same as default_week on Monday)
prev_day = last_monday - _dt.timedelta(days=1)
iso = prev_day.isocalendar()
print(f"{iso.year}-W{iso.week:02d}")
')"     || { log "ERROR: could not compute digest week"; exit 1; }
OUT="$STATE/digests/$WEEK.json"
log "building digest $WEEK -> $OUT"
python3 -m clipsmith.digest build --db "$DB" --week "$WEEK" --out "$OUT" >/dev/null \
    || { log "ERROR: digest build failed"; exit 1; }

# --- 3. optional ship (servexeri bot host) --------------------------------------
if [ "${CLIPSMITH_DIGEST_MODE:-full}" = "build" ] && [ -z "${CLIPSMITH_DIGEST_SHIP_TO:-}" ]; then
    log "ERROR: CLIPSMITH_DIGEST_MODE=build but CLIPSMITH_DIGEST_SHIP_TO is unset — built digest would go nowhere (servexeri poster starves)"
    exit 1
fi
if [ -n "${CLIPSMITH_DIGEST_SHIP_TO:-}" ]; then
    log "shipping digest to $CLIPSMITH_DIGEST_SHIP_TO"
    scp -o BatchMode=yes -o ConnectTimeout=10 \
        -o ServerAliveInterval=5 -o ServerAliveCountMax=3 \
        "$OUT" "$CLIPSMITH_DIGEST_SHIP_TO" \
        || { log "ERROR: digest ship failed"; exit 1; }
fi

# --- 4. post (skipped in build/producer-only mode) -------------------------------
if [ "${CLIPSMITH_DIGEST_MODE:-full}" = "build" ]; then
    log "CLIPSMITH_DIGEST_MODE=build — producer-only run, post is servexeri's job"
    log "digest run finished"
    exit 0
fi
cd "$REPO/bot" || { log "ERROR: bot/ missing in deploy clone"; exit 1; }
export CLIPSMITH_DB="$DB"
if [ "${CLIPSMITH_DIGEST_LIVE:-0}" = "1" ]; then
    log "posting digest LIVE (CLIPSMITH_DIGEST_LIVE=1)"
    node src/post-digest.mjs --digest "$OUT" \
        ${CLIPSMITH_DIGEST_CHANNEL:+--channel "$CLIPSMITH_DIGEST_CHANNEL"} \
        || { log "ERROR: live digest post failed"; exit 1; }
else
    log "posting digest in DRY-RUN (flip CLIPSMITH_DIGEST_LIVE=1 in ~/.config/clipsmith-deploy.env after #12)"
    node src/post-digest.mjs --digest "$OUT" --dry-run \
        || { log "ERROR: dry-run digest post failed"; exit 1; }
fi

log "digest run finished"
