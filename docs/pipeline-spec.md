# OF THE WEEK — pipeline specification

last-verified: 2026-06-10
status: approved spec (source: owner brief 2026-06-10; supersedes nothing — this IS the product definition)

## North star

Increase engagement and energy of the QuakeWorld community over summer (and
permanently) by sharing high-quality, fun content from the community for the
community — automated to the point that it is sustainable over time.

The product: weekly "X OF THE WEEK" branded highlight clips on the
**One More QUAD** YouTube channel (`@OneMoreQUAD`), candidates mined
automatically from fresh 4on4 demos, curated by the owner via Discord, rendered
and assembled hands-off, published after one human approval.

## Cadence

| When | What |
|---|---|
| 3×/week (e.g. Tue/Thu/Sat night) | Detection run: scan all NEW 4on4 demos from the configured match-discovery source since the last watermark, run detectors, persist candidates. Xerial's deployment currently uses a private Turso/libSQL stats database. |
| Monday 20:00 (Europe/Stockholm) | Candidate digest posted to Discord for owner review |
| After owner selection | Render → assemble → upload (unlisted) → review notification |
| After owner approval ("OK" / 👍) | Publish public to the PLAY OF THE WEEK playlist |

## Highlight categories

All windows are expressed in match-relative seconds. "Window" is what gets
rendered; detectors may scan wider context. Minimum 1, maximum 3 candidates
per category per week. If a category has zero qualifying moments, say so in the
digest rather than padding with weak candidates.

### 1. QUAD RUNNER OF THE WEEK
- Window: quad pickup −5 s … +30 s (extend to end of run if frags continue).
- Criteria: the quad run that generated the highest frag count, AND/OR a run
  killing 2+ enemies with rockets (RL).
- Data: `getItems(items=quad)` phases + `getFrags` filtered to holder during
  the held interval (exclude suicides/teamkills); `PowerupEvents[].frags` as
  cross-check.

### 2. TEAM KILLER OF THE WEEK
- Window: ~30 s centred on the incident.
- Criteria (either):
  - 1+ teammates killed with RL or LG within a 30 s window, or
  - 2–4 teammates killed within 0–20 s regardless of weapon.
- Data: `getFrags` entries with `isTeamKill: true`; reconcile with
  `getDemoInfo` `stats.tk` when available.

### 3. PENT GRIND OF THE WEEK
- Window: ~30 s ending shortly after the pent pickup.
- Criteria: the fight for pent that cost the highest number of deaths (both
  teams) before someone actually took it.
- Data: `getItems(items=pent)` phases (`AvailableFrom`→`TakenAt`) joined with
  death events near the pent location in that interval.

### 4. PENT RUNNER OF THE WEEK
- Window: pent pickup −5 s … +30 s.
- Criteria: the pent run that killed the most enemies and/or the most enemies
  carrying weapons (RL/LG carriers weighted higher).
- Data: `PowerupEvents` (pent) + `getFrags` in interval; carrier status via
  `getWeaponPickups` / backpack attribution.

### 5. COME-BACK OF THE WEEK (deferred — Phase 5)
- Criteria: one team has been behind with the opponent's frag lead **increasing
  on average for 2+ minutes**, and a player breaks the trend by killing 2+
  enemy weapon-carriers.
- Data: rolling team score reconstructed from `TimelineAnalysis.FragEvents[]`
  deltas (no pre-baked score timeline exists — must aggregate); trend-break
  join against carrier kills. Acknowledged tricky; deliberately deferred.

## Candidate contract

Every detector emits candidates in one shared schema (the **candidate store**
is the pipeline's spine — every later stage reads/writes it):

```
id, category, detected_at, demo_sha, map, matchdate, teams (names+scores),
pov_player {name, userid}, window {start_s, end_s}, score, metrics {…},
description, hub_url, status: candidate → posted → selected → rendered →
uploaded → approved → published (or rejected / revision_requested;
selected → posted is also legal: Discord re-selection replaces the pick
before render starts — see the Discord review loop)
```

- `description`: human-readable — what happens, whose POV it is, and why it
  was recommended (the metrics that ranked it).
- `hub_url`: deep link to the **online** hub with the right POV and time:
  `https://hub.quakeworld.nu/games?gameId=<id>&from=<sec>&track=<userid>`
  (format proven in local-hub `Share.tsx`/`hooks.ts`). Resolving the hub
  `gameId` from the discovery record's `demo_sha` requires a read-only hub
  lookup — allowed for link resolution only. Match discovery remains owned by
  the configured source adapter; Xerial's current adapter uses private Turso
  data.

## Partial-failure recovery and rerun semantics

The intended unattended property is simple: re-running a failed stage should
converge to the same store state as a clean uninterrupted run, without
duplicates and without silently skipping work. This table is the current
contract; rows marked "planned" are not complete until their owning phase
branches land and add validation evidence.

| Stage | Durable state | Safe rerun behavior | Status |
|---|---|---|---|
| Watcher fetch | `state/watcher-watermark.json`, `state/watcher-inprogress.json`, demo files, `pending-detection.jsonl` / `queue.jsonl` | Crash before batch completion leaves the durable watermark at the pre-run value. Rerun reprocesses the frozen batch, skips already-downloaded demos, and sha-dedupes queue/handoff appends. `--dry-run` writes nothing and advances no watermark. | Implemented in issue #32 watcher lane |
| Detector emission | Candidate store natural key `(category, demo_sha, pov_player.name, window.start_s, window.end_s)` | Rerun re-detects and re-emits; duplicate moments return the existing candidate id and do not overwrite progressed fields such as `status`, `hub_url`, or `description`. Detector `--dry-run` avoids store writes. | Implemented in issue #32 watcher lane |
| Digest build | Digest JSON under state, generated from candidates still in `status=candidate` for the target week | Rebuild is read-only with respect to the candidate store. It may replace the same digest JSON for the same week, but does not flip candidate statuses. | Implemented by #17 |
| Digest post | Discord category messages plus candidate store statuses | Dry-run renders messages only. Live mode flips a candidate `candidate -> posted` only after its category message posts; a failed category leaves its candidates unflipped. Rerunning skips candidates already beyond `candidate` when marking posted. | Implemented by #17 |
| Selection capture | Candidate status `posted -> selected` (re-selection: `selected -> posted` for the replaced pick); `state/render-queue.json` handoff | Selections are serialized per category inside the bot process (the single selection writer), so simultaneous selections cannot double-select; the later one wins and reverts the earlier pick `selected -> posted`. The queue file is rewritten atomically (temp file + rename) with mutations serialized; re-selection marks the replaced pick's pending entry `superseded`, keeping at most one pending render per category. Re-clicking the already-selected candidate is a no-op (no transition, no duplicate queue entry). | Implemented by #18 |
| Render capture | Candidate status `selected -> rendered`; canonical raw capture file | `clipsmith.render` computes the candidate-id raw path before any worker side effects. `--dry-run` prints the plan and writes nothing. If that raw file already exists, a rerun skips the worker and flips `selected -> rendered`; a rerun of an already `rendered` candidate with the raw present is a no-op. `rendered` without the raw errors loudly so the raw is restored or status is re-walked. `produce_clip.py` also resumes from existing raw files before assembly. | Implemented across #19/#26/#32 render slice |
| Upload/publish | Candidate status `rendered -> uploaded -> approved -> published`; YouTube URL | Upload rerun must detect an already-uploaded title/candidate and record the existing URL instead of double-uploading. Publish rerun must preserve the final public state. | Planned in #28/#29/#32 follow-up |

## Discord review loop

- Dedicated Discord server (owner creates — placeholder) with a clipsmith bot.
- Monday 20:00: one digest message per category — labeled
  "QUAD RUNNER OF THE WEEK — candidates", each candidate an embed with
  description, POV, reasoning, and the hub URL for instant review.
- Owner selects one candidate per category (buttons preferred; accept also a
  plain reply). Selection flips status → `selected` and triggers the render
  stage. Re-selecting a different candidate in the same category replaces the
  pick: the previous candidate reverts `selected → posted` and its pending
  render-queue entry is marked `superseded`.
- After upload: notification with the unlisted YouTube link. Owner approves
  with 👍 or "OK" → publish. If the owner opens a thread with feedback instead,
  the pipeline parses the change request, re-renders, and re-posts for review
  (revision loop).

## Render stage

- Input: selected candidate (sha + window + POV).
- Engine: headless ezQuake in the `clipsmith-render` WSL distro (decision-log;
  all documented render gotchas apply: classic GL, autotrack clearing,
  console suppression, ext4 staging).
- **Config parity requirement**: the capture config must match the config the
  online hub uses for demo playback, including the QTV-style **teaminfo**
  overlay showing the spectated team's status AND the enemies' weapons
  (`hud_teaminfo` family — research ticket; this is a hub/QTV-unique element).
- **Nothing from the game view may be cut away** — the branded frame must not
  crop gameplay; scale to fit the frame hole.
- Output settings must be investigated and matched end-to-end:
  capture fps/resolution ↔ Resolve timeline settings ↔ YouTube-optimal
  delivery (current PoC is 1280×720@30 — likely upgrade to 1920×1080@60).

## Assembly stage (DaVinci Resolve)

Decision: the OF THE WEEK product is assembled in **DaVinci Resolve** driven by
the Resolve MCP (supersedes "ffmpeg for now" for this product — see
decision-log entry 2026-06-10). Structure per clip:

1. **Intro**: owner's short branded OneMoreQUAD intro video (placeholder path)
   with owner-chosen music.
2. **Title card**: text "<CATEGORY> OF THE WEEK" + week number, player name
   beneath. Category styling:
   - QUAD RUNNER: name colored like the quad (blue glow), pulsates **once**
     synchronized with the quad pickup sound.
   - PENT RUNNER: same treatment with pentagram color/texture (red).
   - TEAM KILLER: plain name, no pulse.
3. **Transition** into the clip, music continuing.
4. **The clip** inside the branded QUAD RUNNER frame (`Demo Clip Focus` scene
   from `Xerialen/onemorequad-brand` — the only cam-free scene; lower-third
   patched to player · map).
5. **Kill counter**: numbers appearing fast with a fitting SFX for every kill
   in the window (timestamps from analysis). Teamkills get an "oops"-flavored
   effect instead.
6. **Outro**: the intro video reused as outro.

Game audio stays present under the music. Music is owner-chosen per clip
(placeholder: default to a beatforge beat from
`C:\Users\example\Music\beatforge\` until the owner picks).

## Publish stage

- Export from Resolve with YouTube-optimized settings (H.264/AAC 1080p,
  bitrate per channel-art-specs vault doc).
- Upload via **browser automation** (Chrome with the owner's logged-in
  OneMoreQUAD session) to the **PLAY OF THE WEEK** playlist, initially
  unlisted.
- Metadata generated automatically: title, hashtags, description containing —
  what the event is, the player, link to the quake.world Discord, the
  QuakeWorld wiki, how to start playing again (nQuake download), channel
  cross-links.
- Discord notification → approval → flip to public. Feedback thread → revision
  loop (see above).

## Placeholders (owner-dependent, never block the build)

Build everything against these placeholders; each has an HITL issue:

| Placeholder | Used by | Until owner provides |
|---|---|---|
| `CLIPSMITH_DISCORD_TOKEN`, `CLIPSMITH_DISCORD_*` in `~/.config/api-keys.env` | bot | Discord server + bot app created |
| `assets/placeholder/intro.mp4` (any 3–5 s clip) | assembly | real OneMoreQUAD intro video |
| beatforge beat auto-pick | assembly | per-clip music choice mechanism |
| `PLAY OF THE WEEK` playlist id/url | publish | playlist created on channel |
| Chrome profile logged into @OneMoreQUAD | publish | confirmed working session |
| quad/pent pickup sounds (synthesize stand-ins) | title card | sounds extracted from game files |

## Reusable components inventory (don't rebuild)

- Discovery: `scripts/selection/turso_query.py` is the current adapter for
  Xerial's private Turso/libSQL source (stats row → sha → CDN); another source
  can replace this boundary by emitting the normalized game fields.
- Analysis: the `mvd-api` module from
  [MVD Analyzer upstream](https://github.com/galfthan/mvd_analyzer) on :8080,
  plus `scripts/analysis/*` patterns. The current deployment uses
  [Xerialen's fork](https://github.com/Xerialen/mvd_analyzer).
- Render: `scripts/render/run_pov.sh` + cfg trigger pattern + WSL distro.
- Branding assets/techniques: `scripts/branding/*` (fonts, overlay render,
  alpha intro frames).
- Discord bot pattern: `~/projects/bots/Evola-bot` (discord.js v14, systemd
  timer, embeds+buttons, token metering).
- Hub URL format: local-hub `frontend/src/pages/games/player/Share.tsx`.
- Resolve MCP: proven import/timeline/transform/render path (see
  [testing-and-validation.md](testing-and-validation.md); gotchas: `_mcp_` project-name prefix, `require_temp_target:false`, 150-frame
  still cap, cold-launch modal needs one human click).
- YouTube metadata limits: vault `podcasting/channel-art-specs.md`.
