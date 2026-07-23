# clipsmith schedule — pinnacle WSL2 deployment (issue #21)

Systemd timers inside pinnacle's WSL2 **Ubuntu-24.04** (the mvd-api host; the
machine hosting policy puts all services in WSL2, never Windows-native):

| Unit | Schedule | What |
|---|---|---|
| `clipsmith-detect.timer` → `clipsmith-detect.service` | **Tue/Thu/Sat 23:33 Europe/Stockholm**, `Persistent=true` | `clipsmith.watcher` (new Turso 4on4 demos) → the four Phase-1 detectors per demo → `hub_links` resolution pass |
| `clipsmith-digest.timer` → `clipsmith-digest.service` | **Mon 19:30 Europe/Stockholm**, `Persistent=true` | best-effort detection sweep → `clipsmith.digest build` → optional ship to servexeri → `bot/src/post-digest.mjs` (**dry-run until #12**; skipped in `CLIPSMITH_DIGEST_MODE=build`). 19:30 so the build/ship lands BEFORE servexeri's 20:00 poster |
| `clipsmith-failure@.service` | on failure of either | err-priority journal alert (`journalctl -t clipsmith-alert`); Discord alerts are #31 |
| `clipsmith-mvd-api.service` | on demand (not enabled) | mvd-api on :8080, started by the runs only when nothing healthy already listens (the MCP launcher's ad-hoc instance keeps priority) |

Host split (docs/decision-log.md "Schedule hosting"): detection lives HERE
(must co-locate with mvd-api); the bot/digest-post target host is **servexeri**
once #12 goes live — see `../servexeri/README.md`. Until then the digest runs
here in dry-run, so a missed Monday window costs nothing.

**Producer/poster split**: `run-digest.sh` is the ONLY producer of the weekly
digest JSON. At servexeri switchover the pinnacle digest timer STAYS ENABLED
in producer-only mode (`CLIPSMITH_DIGEST_MODE=build` + `CLIPSMITH_DIGEST_SHIP_TO`
in `~/.config/clipsmith-deploy.env`): it sweeps, builds and ships at 19:30;
servexeri posts at 20:00 (with a bounded wait for the shipped file). Never
disable `clipsmith-digest.timer` at switchover — that starves the poster.

**Deploy gate (PR #39)**: `install.sh` probes — twice-emitting the same
candidate into a throwaway db — that `emit_candidate` is natural-key
idempotent (`(category, demo_sha, pov, window)` duplicate = no-op). Without
that (#39 unmerged in the deployed ref) it REFUSES to enable
`clipsmith-detect.timer` and exits 2. The digest run's pre-digest sweep
respects the same gate (it skips the sweep while the detect timer is not
enabled, so nothing detects ungated). The runner's `detect-ledger.json` is a
retry/abandonment **optimization only** — it cannot prevent double-emits when
a run crashes between a detector's emit and the ledger write; store-level
idempotency is the correctness mechanism.

## Layout

- **Deploy clone** defaults to `$HOME/clipsmith` (WSL ext4) and is pinned to a
  `deploy` branch from the repo's `main`; override it with
  `CLIPSMITH_DEPLOY_DIR`. The installer renders unit paths for that location.
- **Canonical state** defaults to `$HOME/.local/share/clipsmith`
  (`candidates.db`, `demos/`, `digests`, watermark); override it with
  `CLIPSMITH_STATE_DIR` when it must be shared with Windows-side phases
  (render, Resolve). Single writer at a time: the two timers are serialized via
  a shared `flock` path. Default:
  `/tmp/clipsmith-${USER:-user}.lock`; override with `CLIPSMITH_LOCK_FILE`
  if the service user needs a different writable path.
- **Runner-owned state** (also under `state/`): `detect-ledger.json` — the
  per-(sha, detector) completion ledger that makes retries exactly-once (the
  store has no dedupe).
- **Credentials**: never in units or this repo. `clipsmith.watcher` and
  `bot/config.mjs` read `~/.config/api-keys.env` (WSL home) themselves.
- **Knobs**: `~/.config/clipsmith-deploy.env` (seeded by install.sh) —
  `CLIPSMITH_DIGEST_LIVE`, `CLIPSMITH_DIGEST_CHANNEL`,
  `CLIPSMITH_DIGEST_SHIP_TO`, `CLIPSMITH_DIGEST_MODE` (`full`/`build`;
  `build` = producer-only, the servexeri switchover), plus path overrides
  (`CLIPSMITH_DEPLOY_DIR/STATE_DIR/DB/API_URL`).

## Install / update

```bash
# From a checkout visible inside WSL:
bash /path/to/clipsmith/scripts/deploy/pinnacle/install.sh
```

Idempotent. Re-run after merging changes to `main` (it refreshes the deploy
clone, re-installs units, daemon-reloads). Pre-merge testing:
`CLIPSMITH_REF=<branch> bash …/install.sh`.

The installer enforces the **#39 deploy gate** (see above): if the deployed
ref's `emit_candidate` is not natural-key idempotent, units are still
installed and the digest timer enabled, but `clipsmith-detect.timer` is left
disabled and the script exits 2 with instructions. Merge PR #39 and re-run.

Note: the very first `enable --now` fires both services once immediately
(`Persistent=true` with no stamp file yet = "missed window"). Harmless —
detection is idempotent and the digest is dry-run.

## Operate

```bash
# status + next elapse times
systemctl list-timers 'clipsmith-*' --no-pager

# force a run NOW (exactly what the timer would do)
sudo systemctl start clipsmith-detect.service
sudo systemctl start clipsmith-digest.service

# watch / inspect
journalctl -u clipsmith-detect -f
journalctl -u clipsmith-digest -n 200
journalctl -t clipsmith-alert            # failure hook lines (err priority)

# pause / resume the schedule (keeps units installed)
sudo systemctl disable --now clipsmith-detect.timer clipsmith-digest.timer
sudo systemctl enable  --now clipsmith-detect.timer clipsmith-digest.timer
```

### Go live (the one line, after #12)

```bash
sed -i 's/^CLIPSMITH_DIGEST_LIVE=0/CLIPSMITH_DIGEST_LIVE=1/' ~/.config/clipsmith-deploy.env
```

Optionally set `CLIPSMITH_DIGEST_CHANNEL=<test-channel-id>` there first for a
safe live test. No daemon-reload needed (the env file is read at service
start).

## Timezone / DST

Both timers pin the zone **inside** the calendar expression
(`OnCalendar=… Europe/Stockholm`), so systemd computes elapses against the
IANA tzdata rules — the CET (+01, winter) / CEST (+02, summer) switch is
handled for free and the schedule stays at local wall-clock time year-round,
even if the host TZ were changed. Verify any expression with:

```bash
systemd-analyze calendar 'Mon *-*-* 20:00:00 Europe/Stockholm'
```

## Missed windows / catch-up (`Persistent=true`)

WSL is regularly killed (`wsl --shutdown` before gaming) and the timers only
run while the distro runs. Both timers set `Persistent=true`: systemd stamps
each trigger in `/var/lib/systemd/timers/stamp-<timer>.timer`; on the next
distro boot, a missed window fires **once, immediately**. So the worst case
after a dead Monday 20:00 is a digest that posts when WSL next comes up.

- The distro boots on any `wsl -d Ubuntu-24.04 …` invocation and stays up
  (systemd keeps the VM alive) until `wsl --shutdown`/Windows reboot.
- After a gaming session, just touching WSL (`wsl -d Ubuntu-24.04 -e true`)
  is enough to trigger catch-up. If you want this automatic at Windows logon,
  a Task Scheduler entry running that command would do it — **not installed**
  (Windows-side change; owner's call).
- Catch-up runs are serialized: if detect + digest both catch up at boot they
  queue on the shared `CLIPSMITH_LOCK_FILE` instead of fighting over the store.

## Failure visibility

Every failed run leaves: the unit's own journal (`journalctl -u
clipsmith-detect`/`-digest`), plus an err-priority line from the `OnFailure=`
hook under `journalctl -t clipsmith-alert`. Partial detection failures are
retried automatically: the watcher freezes its watermark before the first
failed demo, and `run_detectors.py` keeps failed (sha, detector) pairs pending
in `state/pending-detection.jsonl` + `state/detect-ledger.json` for the next
run without re-emitting candidates that already succeeded.

## Uninstall

```bash
bash /mnt/c/Users/example/projects/quakeworld/clipsmith/scripts/deploy/pinnacle/uninstall.sh
```

One command; removes all `clipsmith-*` units and timers. State and the deploy
clone are left in place (the script says what it kept).

## Known gaps / accepted risks

- **SQLite over /mnt/c (9p)**: `candidates.db` lives on the Windows side and
  is written from WSL. Single sequential writer (flock-serialized timers), so
  lock contention is not in play; if corruption is ever observed, move state
  to ext4 + sync (decision-log "Revisit conditions").
- **Demos that never reach the hub** 404 from mvd-api (sha-based analysis
  needs hub.quakeworld.nu). They are retried for `MAX_ATTEMPTS=8` runs
  (~2 weeks at the 4-runs/week cadence: Tue/Thu/Sat + Monday sweep), then
  abandoned with a loud `ABANDONED sha=…` journal line and a permanent record
  in `state/detect-ledger.json`.
- **Sunday games**: detection is Tue/Thu/Sat, so the Monday-20:00 digest would
  miss Sunday demos; the digest service therefore runs a pre-digest detection
  sweep. Demos that reach Turso *after* Monday 20:00 still slip to the next
  week's pool only if their matchdate is in the closed week — visible in the
  store as `status=candidate` rows from a past week (spec-level cadence gap,
  documented in the validation guidance).
- The digest build/post currently runs here (the box that sleeps) — fine
  while dry-run; the servexeri bundle removes that risk at go-live.

## Week contract (round-2 review fix)

The digest producer (`run-digest.sh`) and poster (`run-digest-post.sh`) both
derive the target ISO week from the **most recent Monday slot**, not from
wall-clock "now". This is intentional: a `Persistent=true` systemd catch-up
run that fires on Tuesday for the missed Monday slot must build/post the
PREVIOUS week, not the current one.

**Logic (both scripts):**
- Compute `days_back = weekday_of_today` (0 on Monday, 1 on Tuesday, ...).
- `last_monday = today - days_back`
- Target week = ISO week of `last_monday - 1 day` (= Sunday of the previous week).
- On Monday: `days_back = 0`, result is last Sunday = finished week.
- On Tuesday catch-up: `days_back = 1`, same result = same finished week.

**Cross-host contract**: the producer ships `<week>.json`; the poster waits
for exactly that week's file (`$DIGESTS/$WEEK.json`). The week is computed
independently on both hosts with the same algorithm, so they always agree on
the expected filename regardless of whether either ran on Monday or later.
The poster' bounded wait and loudly-failing mismatch check remain intact.
