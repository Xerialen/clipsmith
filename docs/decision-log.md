# Decision Log

## Render host: throwaway WSL distro, never the Windows display stack

### Date
2026-06-09

### Decision
All rendering runs in an isolated, removable WSL distro (`clipsmith-render`,
Ubuntu 22.04 via `wsl --import`). No IDD/virtual-display drivers, no changes to
Windows GPU/driver state, no packages added to the daily-driver Ubuntu-24.04.

### Alternatives Considered
- Windows-host ezQuake capture: reads back black headless; fixing it needs a
  virtual display driver — **user veto** ("will not risk messing up my shit";
  this is the competitive QW gaming box).
- Docker ubuntu:22.04 container: the original plan, but Docker Desktop turned
  out to be uninstalled; reinstalling is a system-level change. `wsl --import`
  gives identical isolation with zero installs.
- Cloud GPU box: fallback if older Mesa hadn't fixed the SIGSEGV; not needed.

### Evidence
Validation on 2026-06-09 showed that Mesa 23.2 d3d12 renders correctly; Mesa 25.2.8
software GL crashes all engines on both available Linux hosts.

### Expected Consequences
Renders are reproducible and disposable (`wsl --unregister clipsmith-render`);
wall speed is bounded by CPU PNG encode (~8 fps at 720p), not GPU.

### Revisit Conditions
Need for >1080p/60fps renders, or NVENC in the loop — then evaluate
`MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA` (4090 via d3d12) or a cloud box.

**Revisit CLOSED 2026-06-11 (issue #22 benchmark):** the adapter env var was
evaluated head-to-head at 720p30 / 1080p30 / 1080p60 — throughput identical
on the 4090 vs the AMD iGPU at every mode (the PNG capture path is the
bottleneck, not the GPU). No env-var wiring; capture stays on the default
adapter. Full table in `docs/picture-settings.md`.

## Demo selection: Turso only

### Date
2026-06-09

### Decision
Game/demo selection queries the qw-stats Turso DB exclusively (sha256 keys the
`d.quake.world` CDN). The hub Supabase is not used.

### Alternatives Considered
hub.quakeworld.nu Supabase (`searchGames`) — explicitly rejected by the user.

### Evidence
`games` / `player_games` / `player_aliases` schema covers selection needs;
exercised on the HX lookup during validation on 2026-06-10.

### Expected Consequences
One credentialed dependency (`TURSO_*` in `~/.config/api-keys.env`); player-name
edge cases (aliases, QW glyphs, clan-vs-player like "HX") resolved via
`player_aliases` + the vault people layer.

### Revisit Conditions
User says otherwise, or Turso lacks data the pipeline needs (e.g. very fresh
games before the nightly sync).

## Finished clips live in Google Drive, never under C:\nQuake

### Date
2026-06-10

### Decision
Output home is `C:\Users\example\Videos\clipsmith\raw\`, named
`<matchdate>_<map>_<teams>_<situation>_<N>pov[_<speed>].mp4`. The game install
stays clean; PNG intermediates are disposable once the MP4 is in Drive.

### Alternatives Considered
`C:\nQuake\capture\` (engine default under basedir) — user rule against it.

### Evidence
User instruction 2026-06-10; G: is not WSL-mountable, so the flow is
render on ext4 → stage `C:\Users\example\wsl\out\` → PowerShell move to Drive.

### Expected Consequences
Clips sync to cloud automatically (shareable, cross-machine). The orchestrator
must own the rename+move step since the engine can't write there directly.

### Revisit Conditions
Drive quota pressure, or a publish target (e.g. clips.xerious.org) that wants a
different staging layout.

## PoC in scratch first, repo after seams close

### Date
2026-06-09

### Decision
Phase-1 PoC lived in `~/projects/quakeworld/_scratch/clipsmith-poc/` until the
seams were proven; this repo (from `Xerialen/ai-project-template`) was created
2026-06-10 once render + selection + analysis were all validated.

### Alternatives Considered
Repo-first — rejected: too much ceremony while the render seam was still a
research question with a real chance of ending at "cloud GPU or bust".

### Evidence
The render seam took three host attempts (Windows black readback, Mesa 25.2.8
crashes, Docker gone) before landing; none of that churn polluted a repo.

### Expected Consequences
This repo is now canonical; the scratch dir and `C:\Users\example\wsl\*.sh`
copies are superseded by `scripts/` here and will drift — treat repo as truth.

### Revisit Conditions
n/a — completed.

## Branding assembler: ffmpeg for the PoC, Resolve MCP deferred

### Date
2026-06-10

### Decision
Assemble the branded clip (intro + framed capture + audio) in **ffmpeg**, driven
by scripts in `scripts/branding/`. The Resolve MCP path stays available but is
not the canonical assembler for now.

### Alternatives Considered
- **Resolve MCP assembly** (the originally-planned Phase-1 seam): proven able to
  import/timeline/transform/render, and a v2 of this clip was built that way. But
  the brief's "fire burns away to reveal the clip" needs an alpha-erode transition
  that is hard to author and verify via blind Fusion node scripting; the MCP v2
  fell back to a zoom-out reveal (doesn't match the brief).
- ffmpeg: every stage is a deterministic, frame-verifiable file; the fire-burn is
  just alpha PNG-sequence compositing. Matches the brief exactly.

### Evidence
v3 (ffmpeg, fire-burn) matches the user's explicit transition pick and was
verified frame-by-frame; v2 (Resolve MCP, zoom reveal) did not. Both clips kept
side by side. Music switched to own beatforge beats (no licensing) vs a CC-BY
track used in a first pass.

### Expected Consequences
The scripted ffmpeg pipeline is reproducible and CI-friendly; an editable Resolve
project is NOT produced (re-derivable on demand if a human wants to hand-tweak).

### Revisit Conditions
If the brand layout needs interactive human polish per clip, or motion-graphics
beyond what HTML-render + ffmpeg can do cleanly, revisit Resolve/Fusion as the
production assembler.

## OF THE WEEK assembly: DaVinci Resolve via MCP (supersedes "ffmpeg for now" for this product)

### Date
2026-06-10

### Decision
The weekly OF THE WEEK clips are assembled in DaVinci Resolve driven by the
Resolve MCP: owner's intro video, animated title card (category-colored name
pulse + pickup sound), framed clip with kill-counter overlay, intro reused as
outro, YouTube-optimized render preset. ffmpeg stays as the utility compositor
(alpha overlays, intro frame sequences, audio prep) feeding Resolve.

### Alternatives Considered
- Pure ffmpeg assembly (the PoC path): proven and CI-friendly, but the owner's
  brief explicitly specifies Resolve with his existing intro video, and the
  recurring product benefits from an editable project per clip (feedback →
  revision loop).

### Evidence
Owner brief 2026-06-10 (pipeline-spec.md). Resolve MCP path proven in the
2026-06-10 session (import/timeline/stills/transform/render; gotchas in
validation results and the project brief).

### Expected Consequences
Resolve must be running (one-time human click past the cold-launch Project
Manager modal per boot); render jobs are queued via MCP. Each clip leaves an
editable `_mcp_*` project for hand-tweaks and revisions.

### Revisit Conditions
If unattended Monday-night assembly is blocked too often by the Resolve
cold-launch modal, build the ffmpeg fallback assembler from the PoC scripts.

## Hub Supabase allowed for deep-link resolution only

### Date
2026-06-10

### Decision
Candidate selection stays Turso-only, but resolving the ONLINE hub `gameId`
from a Turso `demo_sha` (to build review URLs
`https://hub.quakeworld.nu/games?gameId=…&from=…&track=…`) may query the hub
Supabase read-only. Narrow exception to the "Turso only" decision above.

### Alternatives Considered
Skipping hub links (owner reviews raw demos — rejected, the brief requires
one-click hub review at the right POV and time).

### Evidence
Hub URL format proven in local-hub `Share.tsx`/`hooks.ts`; hub Supabase
exposes `demo_sha256` for the mapping (mvd-mcp `supabase.go`).

### Expected Consequences
One unauthenticated read-only HTTP dependency at digest-build time; selection
logic itself remains Turso-pure.

### Revisit Conditions
Hub schema change breaking sha lookup, or Turso starts carrying hub game ids.

## Schedule hosting: detection on pinnacle WSL2; bot/digest target servexeri (interim digest on pinnacle, dry-run)

### Date
2026-06-11

### Decision
The issue-#21 host split:
- **Detection (Tue/Thu/Sat 23:33 Europe/Stockholm)** runs on **pinnacle WSL2
  Ubuntu-24.04** as `clipsmith-detect.timer` with `Persistent=true` — it MUST
  co-locate with mvd-api (:8080), and the machine hosting policy puts all
  services in WSL2.
- **Bot + Monday-20:00 digest post** belong on **servexeri** (always-on LAN
  hub; pinnacle sleeps and gets `wsl --shutdown` for gaming, so a Monday
  post from pinnacle is a silently-late product). servexeri lacks mvd-api,
  which is fine — posting needs no analysis.
- **Interim**: until #12 (Discord server/token) exists, live posting is
  impossible anyway, so a `clipsmith-digest.timer` runs on pinnacle in
  `--dry-run` (Persistent catch-up; one env line, `CLIPSMITH_DIGEST_LIVE=1`
  in `~/.config/clipsmith-deploy.env`, flips it live). The servexeri bundle
  (`scripts/deploy/servexeri/`) is complete; switchover = install there +
  two env lines on pinnacle (`CLIPSMITH_DIGEST_SHIP_TO`,
  `CLIPSMITH_DIGEST_MODE=build`).
- **Producer/poster split** (2026-06-11 review amendment):
  pinnacle's digest run is the ONLY producer of the weekly digest JSON, so
  its timer is NEVER disabled at switchover — `CLIPSMITH_DIGEST_MODE=build`
  turns it producer-only (sweep + build + ship, no local post). Ordering:
  producer fires **Mon 19:30** Europe/Stockholm, servexeri's poster **Mon
  20:00**, and the poster additionally waits (bounded,
  `CLIPSMITH_DIGEST_WAIT_S`, default 20 min, JSON-parse check against torn
  ships) before refusing loudly.
- **Deploy gate / ledger semantics** (same amendment): the runner's
  `detect-ledger.json` is a retry/abandonment **optimization only** — a
  crash between a detector's emit and the ledger write replays that detector,
  so correctness against double-emits requires `emit_candidate` natural-key
  idempotency (PR #39). `install.sh` probes for that behavior at install
  time (duplicate emit on a throwaway db must be a no-op) and refuses to
  enable `clipsmith-detect.timer` until #39 is merged into the deployed ref.
  Persistence ordering in `run_detectors.py` also closes the cheap window:
  on full success the pending removal is made durable BEFORE the ledger
  entry is dropped.

**Cross-host store access** (candidates.db is local SQLite on pinnacle):
pinnacle stays the **single writer**; the **digest JSON is the cross-host
contract**. Pinnacle's digest run builds `state/digests/<week>.json` (after a
pre-digest detection sweep that also closes the Sunday-games gap) and ships it
via scp (`CLIPSMITH_DIGEST_SHIP_TO`); servexeri posts the shipped file and
refuses loudly if the expected week is missing. Store write-backs
(candidate→posted flips) and bot reads execute ON pinnacle through
`CLIPSMITH_PYTHON=store-python-ssh.sh` (bot/store.mjs already shells to
`$CLIPSMITH_PYTHON -m clipsmith.store`), so no code changes; pinnacle-down
flip failures are visible (exit 1 → OnFailure) and recoverable via
`clipsmith.digest mark-posted`.

### Alternatives Considered
- **Everything on pinnacle permanently**: simplest, but Monday 20:00 from a
  box that is regularly `wsl --shutdown` means late digests forever;
  acceptable only while posts are dry-run (hence the interim).
- **Everything on servexeri**: impossible — detection needs mvd-api (and the
  4090 box's bundle); porting mvd-api was out of scope and unnecessary.
- **Replicating/syncing candidates.db to servexeri** (rsync/litestream):
  creates a second writer or a stale replica; rejected to keep SQLite
  single-writer semantics.
- **Bot remote, store over HTTP API**: would need a new service + auth on a
  box that sleeps; the SSH-wrapper reuses existing injectable seams instead.

### Evidence
mvd-api confirmed WSL-only (`ss -tlnp` → `mvd-api -addr :8080`, healthz
schemaVersion 12); WSL TZ Europe/Stockholm; existing production state at the
Windows repo `state/` (candidates.db, watermark) reused as canonical;
timers, forced runs, and catch-up behavior validated on 2026-06-11.

### Expected Consequences
Detection code runs from an ext4 deploy clone pinned to `main`
(`/home/clipsmith/projects/clipsmith`), never from the Windows working tree's
checked-out branch; state stays on `/mnt/c` (shared with Windows-side render
phases) with flock-serialized single-writer access. Missed windows replay
once on next WSL boot (`Persistent=true`). servexeri install is an owner
manual step (this repo's automation never touches remote machines).

### Revisit Conditions
SQLite-over-9p corruption observed (move state to ext4 + sync); servexeri→
pinnacle-WSL SSH proving impractical (then: queue flips on servexeri and
reconcile from pinnacle); Turso growing hub game ids; or detection becoming
hostable on servexeri (mvd-api ported).
