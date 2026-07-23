#!/usr/bin/env bash
# clipsmith schedule install — pinnacle WSL2 Ubuntu-24.04 (issue #21).
# Run inside WSL as the service account (will sudo for the unit install):
#   bash /path/to/clipsmith/scripts/deploy/pinnacle/install.sh
#
# What it does (idempotent — re-run to update after a merge):
#   1. clone/refresh the deploy clone at $CLIPSMITH_DEPLOY_DIR from the
#      Windows repo's git (default ref: main) — production code never tracks
#      whatever branch the Windows working tree happens to have checked out
#   2. npm ci in bot/ (best effort — only needed for LIVE posting, post-#12)
#   3. seed ~/.config/clipsmith-deploy.env (the live toggle) if absent
#   4. DEPLOY GATE: probe (on a temp db) that emit_candidate is natural-key
#      idempotent (PR #39). The detect-ledger is only a retry/abandonment
#      optimization — correctness against double-emits on crash/replay comes
#      from the store-level idempotency, so without it the detect timer is
#      REFUSED (left disabled) and this script exits 2.
#   5. install + enable the systemd units/timers (sudo); clipsmith-detect.timer
#      only if the gate passed
#
# Env overrides:
#   CLIPSMITH_DEPLOY_DIR  (default $HOME/clipsmith)
#   CLIPSMITH_REPO_SOURCE (default: repository containing this script)
#   CLIPSMITH_REF         (default main — set to a branch for pre-merge testing)
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${CLIPSMITH_REPO_SOURCE:-$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)}"
DEPLOY="${CLIPSMITH_DEPLOY_DIR:-$HOME/clipsmith}"
REF="${CLIPSMITH_REF:-main}"
UNITS="$DEPLOY/scripts/deploy/pinnacle/units"
SERVICE_USER="${CLIPSMITH_SERVICE_USER:-$(id -un)}"
MVD_DIR="${CLIPSMITH_MVD_DIR:-$HOME/mvd-mcp-bundle}"

echo "==> deploy clone: $DEPLOY (ref $REF from $SOURCE)"
if [ ! -d "$DEPLOY/.git" ]; then
    git clone --no-checkout "$SOURCE" "$DEPLOY"
fi
git -C "$DEPLOY" fetch origin "$REF"
git -C "$DEPLOY" checkout -B deploy FETCH_HEAD
git -C "$DEPLOY" log --oneline -1

echo "==> bot npm ci (best effort; required only for live posting)"
if (cd "$DEPLOY/bot" && npm ci --omit=dev --no-audit --no-fund); then
    echo "    bot dependencies installed"
else
    echo "    WARN: npm ci failed — dry-run digest still works (lazy discord.js import)"
fi

ENVFILE="$HOME/.config/clipsmith-deploy.env"
if [ ! -f "$ENVFILE" ]; then
    echo "==> seeding $ENVFILE"
    mkdir -p "$HOME/.config"
    cat > "$ENVFILE" <<'EOF'
# clipsmith schedule knobs (issue #21). Loaded by clipsmith-detect.service and
# clipsmith-digest.service. No secrets here — credentials stay in
# ~/.config/api-keys.env, read directly by the clipsmith CLIs.
#
# THE go-live line (flip after the owner completes #12 — Discord server + bot):
CLIPSMITH_DIGEST_LIVE=0
# Optional: post the first live digest to a test channel instead of the
# review channel from api-keys.env:
#CLIPSMITH_DIGEST_CHANNEL=<test-channel-id>
# Switchover to the servexeri poster (see scripts/deploy/servexeri/README.md):
# uncomment BOTH lines — ship the built digest JSON there AND drop the local
# post (producer-only mode). KEEP clipsmith-digest.timer enabled: this run is
# the only producer of the digest JSON servexeri posts.
#CLIPSMITH_DIGEST_SHIP_TO=<user>@<bot-host>:clipsmith/digests/
#CLIPSMITH_DIGEST_MODE=build
EOF
else
    echo "==> $ENVFILE already exists — left untouched"
fi

echo "==> deploy gate: probing emit_candidate natural-key idempotency (PR #39)"
# Behavioral probe, not a version check: emit the same candidate twice into a
# throwaway db. Idempotent (#39) => 1 row, same id both times. Pre-#39 =>
# 2 rows. The detection schedule is only safe to enable with the former: the
# detect-ledger is a retry/abandonment optimization and does NOT prevent
# double-emits when a run crashes between a detector's emit and the ledger
# write (see run_detectors.py docstring).
GATE_OK=0
if (cd "$DEPLOY" && python3 - <<'PYEOF'
import sys, tempfile
from pathlib import Path
from clipsmith import store
from clipsmith.detectors.common import emit_candidate

CAND = {
    "category": "QUAD RUNNER",
    "demo_sha": "deploy-gate-probe-sha",
    "map": "dm3",
    "matchdate": "2026-01-01",
    "teams": [{"name": "red", "score": 1}, {"name": "blue", "score": 0}],
    "pov_player": {"name": "gate-probe", "userid": 0},
    "window": {"start_s": 1.0, "end_s": 2.0},
    "score": 1,
    "metrics": {},
    # Hand-written (non-stub) description: keeps the describer out of the probe.
    "description": "install-time idempotency gate probe; never reaches a real store.",
    "hub_url": "",
}

with tempfile.TemporaryDirectory() as td:
    db = str(Path(td) / "gate-probe.db")
    id1 = emit_candidate(dict(CAND), db_path=db)
    id2 = emit_candidate(dict(CAND), db_path=db)
    rows = store.list_candidates(db_path=db)
    if len(rows) == 1 and id1 == id2:
        print(f"    gate PASS: duplicate emit was a no-op (1 row, id {id1})")
        sys.exit(0)
    print(f"    gate FAIL: duplicate emit created {len(rows)} row(s) "
          f"(ids {id1!r} vs {id2!r}) — emit_candidate is NOT natural-key idempotent")
    sys.exit(1)
PYEOF
); then
    GATE_OK=1
fi

echo "==> installing systemd units (sudo)"
chmod +x "$DEPLOY"/scripts/deploy/pinnacle/*.sh
UNIT_TMP="$(mktemp -d)"
trap 'rm -rf "$UNIT_TMP"' EXIT
render_unit() {
    local src="$1" dst="$UNIT_TMP/$(basename "$1")"
    sed -e "s|@CLIPSMITH_USER@|$SERVICE_USER|g" \
        -e "s|@CLIPSMITH_HOME@|$HOME|g" \
        -e "s|@CLIPSMITH_DEPLOY_DIR@|$DEPLOY|g" \
        -e "s|@CLIPSMITH_MVD_DIR@|$MVD_DIR|g" \
        "$src" > "$dst"
    sudo install -m 0644 "$dst" /etc/systemd/system/
}
for unit in \
    clipsmith-detect.service clipsmith-detect.timer \
    clipsmith-digest.service clipsmith-digest.timer \
    clipsmith-failure@.service clipsmith-mvd-api.service; do
    render_unit "$UNITS/$unit"
done
sudo systemctl daemon-reload
sudo systemctl enable --now clipsmith-digest.timer

if [ "$GATE_OK" = "1" ]; then
    sudo systemctl enable --now clipsmith-detect.timer
else
    sudo systemctl disable --now clipsmith-detect.timer 2>/dev/null || true
    echo "!!> DEPLOY GATE FAILED: emit_candidate at ref '$REF' is not natural-key"
    echo "!!> idempotent (PR #39 unmerged?). clipsmith-detect.timer LEFT DISABLED —"
    echo "!!> the ledger alone cannot prevent double-emits on crash/replay."
    echo "!!> Merge PR #39 into '$REF', then re-run this installer."
fi

echo "==> done. Verify:"
systemctl list-timers clipsmith-detect.timer clipsmith-digest.timer --no-pager
[ "$GATE_OK" = "1" ] || exit 2
