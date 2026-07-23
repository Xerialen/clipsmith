# clipsmith bot host — servexeri deployment bundle (issue #21)

**Status: NOT installed. Owner manual step.** This repo's automation never
touches servexeri; everything here was written and locally lint-checked on
pinnacle, but has NOT been executed on servexeri.

## Why servexeri

Per docs/decision-log.md "Schedule hosting": detection must co-locate with
mvd-api on pinnacle WSL2, but pinnacle sleeps and gets `wsl --shutdown` for
gaming — a Monday-20:00 digest from a dead WSL is a silently broken product.
servexeri is the always-on LAN hub (Evola-bot already lives there), so the
**owner-facing pieces** (the interactive review bot, the Monday digest post)
move here when #12 (Discord server + token) goes live. Until then the
pinnacle interim digest timer (dry-run) covers the loop.

## Cross-host store design (recorded in decision-log)

`state/candidates.db` is a local SQLite file on pinnacle and STAYS there
(single writer; never replicated):

1. **Digest JSON is the cross-host contract.** Pinnacle builds
   `state/digests/<week>.json` on its own timer — the PRODUCER run, **Mon
   19:30 Europe/Stockholm**, which stays enabled after switchover in
   producer-only mode (`CLIPSMITH_DIGEST_MODE=build`) — and ships it here
   (`CLIPSMITH_DIGEST_SHIP_TO=<user>@<bot-host>:clipsmith/digests/`
   in the producer's `~/.config/clipsmith-deploy.env`). The producer's pre-digest
   sweep makes the shipped file final before the post.
2. **servexeri posts** via `clipsmith-digest-post.timer` (Mon 20:00
   Europe/Stockholm, `Persistent=true`) — 30 min after the producer fires.
   The poster waits (bounded, `CLIPSMITH_DIGEST_WAIT_S`, default 20 min) for
   the expected week's file to arrive and parse as JSON; if it never does
   (pinnacle down, ship failed), the run FAILS LOUDLY
   (`journalctl -t clipsmith-alert`) rather than posting a stale week.
3. **Store writes go back over SSH.** `bot/src/store.mjs` shells out to
   `$CLIPSMITH_PYTHON -m clipsmith.store …`; `bin/store-python-ssh.sh`
   points that at pinnacle over SSH, so the candidate→posted flips and the
   bot's reads execute where the db lives. The wrapper is bounded: whole-call
   `timeout` (`CLIPSMITH_SSH_TIMEOUT_S`, default 120 s), keepalives
   (`ServerAliveInterval=5/CountMax=3`), `BatchMode` + `ConnectTimeout=10`,
   and it validates `CLIPSMITH_PINNACLE_SSH` as a plain `[user@]host` (a
   leading `-` or junk is rejected, exit 64) — so a hung link can never wedge
   the bot. Pinnacle down ⇒ flips fail visibly (logged to stderr) and are
   recoverable on pinnacle with
   `python -m clipsmith.digest mark-posted --week <week>`.

## Prerequisites (owner)

- node ≥ 22 and git on servexeri (Evola-bot box — likely already true).
- SSH key auth servexeri → pinnacle's **WSL** (`BatchMode`): either sshd
  inside Ubuntu-24.04, or Windows OpenSSH with a forced
  `wsl -d Ubuntu-24.04 …` wrapper. **Open question — verify before go-live**;
  test with:
  `ssh <user>@<store-host> "cd clipsmith && python3 -m clipsmith.store list --status candidate | head -1"`
- After #12: `CLIPSMITH_DISCORD_TOKEN/_GUILD_ID/_REVIEW_CHANNEL_ID` in
  servexeri's `~/.config/api-keys.env`.

## Install (on servexeri)

```bash
bash <(git archive --remote=git@github.com:Xerialen/clipsmith.git main \
      scripts/deploy/servexeri/install.sh | tar -xO) \
  || { git clone git@github.com:Xerialen/clipsmith.git "$HOME/clipsmith" && \
       bash "$HOME/clipsmith/scripts/deploy/servexeri/install.sh"; }
```

(or just clone and run `scripts/deploy/servexeri/install.sh`). Then on
**pinnacle**, switch the digest run to producer-only mode by uncommenting
BOTH switchover lines in `~/.config/clipsmith-deploy.env`:

```bash
# producer, at switchover (~/.config/clipsmith-deploy.env):
CLIPSMITH_DIGEST_SHIP_TO=<user>@<bot-host>:clipsmith/digests/
CLIPSMITH_DIGEST_MODE=build
```

**Do NOT disable pinnacle's `clipsmith-digest.timer`** — its run is the only
producer of the digest JSON this host posts (sweep + build + ship at Mon
19:30). `CLIPSMITH_DIGEST_MODE=build` drops only the local post; disabling
the timer would leave the 20:00 poster here waiting on a file nobody ships
(it then fails loudly after `CLIPSMITH_DIGEST_WAIT_S`).

## Operate

```bash
systemctl list-timers clipsmith-digest-post.timer --no-pager
sudo systemctl start clipsmith-digest-post.service     # force a post now
sudo systemctl enable --now clipsmith-bot.service      # after #12
journalctl -u clipsmith-digest-post -n 100
journalctl -t clipsmith-alert
```

Go live: `CLIPSMITH_DIGEST_LIVE=1` in `~/.config/clipsmith-deploy.env`
(same one-line gate as pinnacle).

## Uninstall

```bash
sudo systemctl disable --now clipsmith-digest-post.timer clipsmith-bot.service
sudo rm -f /etc/systemd/system/clipsmith-{bot,digest-post}.service \
           /etc/systemd/system/clipsmith-digest-post.timer \
           /etc/systemd/system/clipsmith-failure@.service
sudo systemctl daemon-reload
```
