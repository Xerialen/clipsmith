#!/usr/bin/env bash
# clipsmith bot/digest-post install — servexeri (issue #21 host split).
# OWNER MANUAL STEP: run this on the bot host as its service account. Nothing
# in the repository installs to a remote host automatically.
#
# Prerequisites (see README.md):
#   - node >= 22 (Evola-bot already requires node on this box)
#   - git access to Xerialen/clipsmith (or rsync the repo over)
#   - SSH from servexeri -> pinnacle WSL (BatchMode key) for store access
#   - CLIPSMITH_DISCORD_* keys in ~/.config/api-keys.env (after #12)
set -euo pipefail

DEPLOY="${CLIPSMITH_DEPLOY_DIR:-$HOME/clipsmith}"
REPO_URL="${CLIPSMITH_REPO_URL:-git@github.com:Xerialen/clipsmith.git}"
REF="${CLIPSMITH_REF:-main}"
UNITS="$DEPLOY/scripts/deploy/servexeri/units"
SERVICE_USER="${CLIPSMITH_SERVICE_USER:-$(id -un)}"

echo "==> deploy clone: $DEPLOY (ref $REF)"
if [ ! -d "$DEPLOY/.git" ]; then
    git clone --no-checkout "$REPO_URL" "$DEPLOY"
fi
git -C "$DEPLOY" fetch origin "$REF"
git -C "$DEPLOY" checkout -B deploy FETCH_HEAD
git -C "$DEPLOY" log --oneline -1

echo "==> bot npm ci"
(cd "$DEPLOY/bot" && npm ci --omit=dev --no-audit --no-fund)

mkdir -p "$DEPLOY/digests"   # pinnacle ships state/digests/<week>.json here

ENVFILE="$HOME/.config/clipsmith-deploy.env"
if [ ! -f "$ENVFILE" ]; then
    echo "==> seeding $ENVFILE"
    mkdir -p "$HOME/.config"
    cat > "$ENVFILE" <<EOF
# clipsmith bot-host knobs (issue #21). No secrets — Discord creds live in
# ~/.config/api-keys.env, read by bot/config.mjs directly.
CLIPSMITH_DIGEST_LIVE=0
#CLIPSMITH_DIGEST_CHANNEL=<test-channel-id>
# Max wait for pinnacle's 19:30 producer to ship the week's digest (seconds):
#CLIPSMITH_DIGEST_WAIT_S=1200
# Store access (pinnacle is the single store owner):
CLIPSMITH_PYTHON=$DEPLOY/scripts/deploy/servexeri/bin/store-python-ssh.sh
# Required before live use:
#CLIPSMITH_PINNACLE_SSH=<user>@<store-host>
# Whole-call ceiling for remote store calls (seconds):
#CLIPSMITH_SSH_TIMEOUT_S=120
EOF
else
    echo "==> $ENVFILE already exists — left untouched"
fi

echo "==> installing systemd units (sudo)"
chmod +x "$DEPLOY"/scripts/deploy/servexeri/bin/*.sh
UNIT_TMP="$(mktemp -d)"
trap 'rm -rf "$UNIT_TMP"' EXIT
render_unit() {
    local src="$1" dst="$UNIT_TMP/$(basename "$1")"
    sed -e "s|@CLIPSMITH_USER@|$SERVICE_USER|g" \
        -e "s|@CLIPSMITH_HOME@|$HOME|g" \
        -e "s|@CLIPSMITH_DEPLOY_DIR@|$DEPLOY|g" \
        "$src" > "$dst"
    sudo install -m 0644 "$dst" /etc/systemd/system/
}
for unit in \
    clipsmith-bot.service clipsmith-digest-post.service \
    clipsmith-digest-post.timer clipsmith-failure@.service; do
    render_unit "$UNITS/$unit"
done
sudo systemctl daemon-reload
# The long-running bot only makes sense once #12 provides a token — enable the
# timer now (dry-run safe), leave the bot for the owner:
sudo systemctl enable --now clipsmith-digest-post.timer
echo "    NOTE: enable the interactive bot after #12 with:"
echo "      sudo systemctl enable --now clipsmith-bot.service"

echo "==> done. Verify:"
systemctl list-timers clipsmith-digest-post.timer --no-pager
