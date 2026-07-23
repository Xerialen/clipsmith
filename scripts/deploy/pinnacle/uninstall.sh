#!/usr/bin/env bash
# clipsmith schedule removal — undoes install.sh (issue #21). Run inside WSL:
#   bash /mnt/c/Users/example/projects/quakeworld/clipsmith/scripts/deploy/pinnacle/uninstall.sh
#
# Leaves intact: the deploy clone, ~/.config/clipsmith-deploy.env, all state
# (candidates.db / demos / digests on the Windows side). Delete those by hand
# if you really want a scorched-earth removal.
set -uo pipefail

sudo systemctl disable --now clipsmith-detect.timer clipsmith-digest.timer 2>/dev/null
sudo systemctl stop clipsmith-detect.service clipsmith-digest.service \
    clipsmith-mvd-api.service 2>/dev/null
sudo rm -f \
    /etc/systemd/system/clipsmith-detect.service \
    /etc/systemd/system/clipsmith-detect.timer \
    /etc/systemd/system/clipsmith-digest.service \
    /etc/systemd/system/clipsmith-digest.timer \
    /etc/systemd/system/clipsmith-failure@.service \
    /etc/systemd/system/clipsmith-mvd-api.service
sudo systemctl daemon-reload
sudo systemctl reset-failed 'clipsmith-*' 2>/dev/null

echo "clipsmith units removed. Remaining (should be empty):"
systemctl list-unit-files 'clipsmith-*' --no-pager
