# clipsmith

**clipsmith turns fresh QuakeWorld demos into curated, branded highlight videos.**

It scans new 4on4 matches, detects interesting moments, posts a weekly shortlist
to Discord, renders the selected POV, assembles the final video in DaVinci
Resolve, and prepares it for publishing.

> [!NOTE]
> clipsmith is an active, environment-specific production project—not yet a
> turnkey desktop application. The detector and store layers are portable; the
> current render pipeline targets Windows 11 with WSL2.

## How the pipeline works

clipsmith is a refinement pipeline. Each stage removes data that is irrelevant
to the next stage and adds the context the next stage needs:

```text
Xerial's private stats database (Turso/libSQL today; replaceable)
  game rows + demo SHA
              │
              ▼
incremental watcher ──► public demo CDN ──► compressed MVD bytes
              │
              ▼
MVD Analyzer / mvd-api
  structured frags, items, players, backpacks, and timeline events
              │
              ▼
highlight detectors
  scored candidate windows with a POV and human-readable explanation
              │
              ▼
SQLite candidate store + public hub-link enrichment
  durable identity, lifecycle state, and review context
              │
              ▼
weekly digest
  at most three ranked candidates per category
              │
              ▼
Discord review
  one editorially selected candidate per category
              │
              ▼
headless ezQuake render
  selected demo interval → clean POV video and game audio
              │
              ▼
title card + branding + kill-counter assets
  raw capture → editorial package
              │
              ▼
DaVinci Resolve assembly
  timed tracks → final encoded video
              │
              ▼
upload, approval, and publication (in progress)
```

### 1. Discover matches from a replaceable stats source

The production deployment starts with **Xerial's private stats database**, a
personal QuakeWorld match index currently hosted on Turso/libSQL. It is not a
public clipsmith service and repository users are not expected to have access
to it. The database supplies lightweight discovery metadata—arrival order,
mode, demo SHA-256, map, match date, teams, and scores—but not the detailed
event stream used to judge highlights.

The watcher queries only newly arrived 4on4 rows after a durable watermark.
That reduces a large match archive to a small incremental batch and gives the
next stage the demo hash needed for acquisition. A failed batch does not advance
the watermark, so rerunning does not silently lose matches.

Turso is an implementation choice, not a product requirement. The current
`clipsmith.watcher` contains a Turso-specific SQL adapter, but it is a
**replaceable discovery adapter**. Another database, public API, queue, or local
manifest can take its place if it emits the same normalized fields and provides
a stable cursor or equivalent replay mechanism. Replacing it currently requires
adapting the watcher query boundary; it is not yet a plug-in selected only by
configuration.

**Input:** private match-index rows.

**Output:** normalized new-game records containing the demo SHA and match
metadata.

### 2. Acquire demos and create a durable handoff

For each discovered hash, the watcher obtains the corresponding compressed MVD
from the QuakeWorld demo CDN (`d.quake.world`). Existing files are reused. A
SHA-deduplicated `pending-detection.jsonl` handoff separates acquisition from
analysis, which means detector failures can be retried without querying the
stats source or downloading the demo again.

This stage turns a database reference into the immutable source artifact used
by every later technical stage.

**Input:** normalized game record with SHA-256.

**Output:** local `.mvd.gz` bytes plus a durable pending-analysis entry.

### 3. Turn MVD bytes into structured game facts

The analysis engine is
[MVD Analyzer](https://github.com/galfthan/mvd_analyzer), whose `mvd-api`
module exposes parsed results over HTTP. clipsmith currently develops and tests
against [Xerialen's fork](https://github.com/Xerialen/mvd_analyzer). Linking
both matters: the upstream repository explains the canonical project, while
the fork identifies the integration actually used by this deployment.

MVD Analyzer converts the binary demo into semantic data such as rosters,
frags, weapons, powerup phases, backpacks, map events, and time-based views.
clipsmith therefore does not need to understand the MVD binary format. Its
responsibility begins at the structured analysis contract.

This is the main semantic refinement step: opaque replay bytes become facts
that detectors can query and compare.

**Input:** compressed MVD demo.

**Output:** structured match and timeline data through the `mvd-api` REST
surface.

### 4. Detect and rank possible highlights

Category-specific detectors combine the structured facts into moments worth
reviewing. For example, the quad detector joins a quad-held interval to enemy
frags, while the pent-grind detector examines deaths around an available pent
before pickup.

Every detector reduces a full match to a small candidate object containing:

- category and numerical score;
- exact match-relative start and end times;
- the POV player and user ID;
- metrics explaining the rank;
- match metadata and demo SHA;
- a natural-language description of what happened and why it matters.

Detectors propose; they do not make the final editorial decision. Weak moments
can be omitted rather than filling a quota.

**Input:** structured events for one match.

**Output:** scored, explainable candidate windows.

### 5. Persist identity, lifecycle, and review context

Candidates are written to a local SQLite store with a natural deduplication key,
so replaying a watcher or detector run converges on the same candidate instead
of creating copies. A separate, read-only hub lookup can map the demo SHA to a
public QuakeWorld Hub deep link at the correct time and POV. The hub enriches a
candidate for human inspection; it is not the match-discovery source.

The store is the pipeline's spine. It records both the candidate data and its
validated lifecycle:

```text
candidate → posted → selected → rendered → uploaded → approved → published
```

Candidates can also be rejected, returned for revision, or replaced before
rendering. Downstream stages consume explicit states instead of inferring work
from files or timestamps.

**Input:** detector result, optionally enriched with a hub link.

**Output:** one durable candidate with identity, explanation, and lifecycle
state.

### 6. Reduce a week to a reviewable digest

The digest builder selects candidates whose match dates fall in the target ISO
week, groups them by category, sorts by detector score, and keeps at most three
per category. It preserves the explanation, POV, metrics summary, and hub link.
An empty category is represented explicitly instead of being padded with a weak
candidate.

This stage changes the unit of work from "all detected moments" to "a short
weekly editorial decision set."

**Input:** all new candidate-state rows for the week.

**Output:** deterministic weekly digest JSON.

### 7. Apply human editorial selection in Discord

The Discord bot renders the digest as one category message with candidate
embeds and selection controls. Posting successfully moves candidates from
`candidate` to `posted`. Selecting one moves it to `selected` and writes a
durable render-queue entry; selecting a replacement safely supersedes the
previous choice.

Automation has now reduced thousands of raw game events to a few options, but a
human still decides what is entertaining and suitable for the channel.

**Input:** weekly digest JSON.

**Output:** one selected candidate per category and an explicit render handoff.

### 8. Reconstruct the selected moment as video

The render orchestrator translates match-relative candidate times into the demo
timeline, adds preroll, locks the requested POV, and drives headless ezQuake in
a dedicated WSL2 render environment. It captures clean frames and game audio,
then assembles a raw video master with FFmpeg.

This stage turns the abstract selection—demo hash, time window, and player—into
the audiovisual evidence viewers will see.

**Input:** selected candidate plus cached demo.

**Output:** canonical raw POV video; candidate state becomes `rendered`.

### 9. Add editorial meaning and brand assets

The assembly preparation stage generates a title card, frame overlay,
kill-counter animation, synchronized sound effects, and publication metadata.
These assets derive from the same candidate data, so names, category, timing,
and counters stay aligned with the selected game window.

The raw replay is now transformed into an editorial package: the viewer can
immediately understand the category, player, and progression of the moment.

**Input:** rendered video plus candidate metrics.

**Output:** timed visual/audio assets and descriptive metadata.

### 10. Assemble, review, and publish

DaVinci Resolve places the intro, title card, raw capture, overlays, music,
game audio, and outro on a deterministic timeline and exports the final video.
The intended final stages upload it unlisted, ask for human approval, and only
then publish it and advance the candidate to `published`. Upload and publication
automation are still in progress.

This final boundary separates a technically complete render from an approved
public release.

**Input:** raw capture and generated editorial assets.

**Output:** reviewable final video, eventually an approved public publication.

## Highlight categories

| Category | What clipsmith looks for |
|---|---|
| **QUAD RUNNER OF THE WEEK** | High-frag quad runs, especially runs with multiple rocket kills |
| **TEAM KILLER OF THE WEEK** | Concentrated teamkill incidents involving RL/LG or multiple teammates |
| **PENT GRIND OF THE WEEK** | The bloodiest fight around an available pent before pickup |
| **PENT RUNNER OF THE WEEK** | Pent runs with the most kills and valuable weapon-carrier kills |
| **COME-BACK OF THE WEEK** | Experimental detection of a player breaking a sustained negative score trend |

Detection windows, ranking rules, and candidate contracts are documented in
[`docs/pipeline-spec.md`](docs/pipeline-spec.md).

## Current status

| Component | Status |
|---|---|
| Demo discovery and download | Implemented for Xerial's private Turso/libSQL source; replaceable at the watcher boundary |
| Candidate store and lifecycle | Implemented |
| Quad, pent, teamkill, and comeback detectors | Implemented |
| Weekly digest generation | Implemented |
| Discord review and selection | Implemented |
| Headless ezQuake rendering | Implemented |
| Title cards, branding, and kill-counter overlays | Implemented |
| DaVinci Resolve assembly | Implemented |
| YouTube upload and final publication | In progress |

See [`docs/roadmap.md`](docs/roadmap.md) for the remaining delivery plan.

## Repository layout

```text
clipsmith/                  Core Python package
  detectors/                Highlight detectors and shared analysis logic
bot/                        Discord review bot (Node.js / discord.js)
scripts/
  analysis/                 MVD Analyzer investigation and timeline tools
  assembly/                 Resolve, title-card, and kill-counter assembly
  branding/                 Brand scene and overlay rendering
  deploy/                   systemd deployment bundles
  render/                   ezQuake capture worker and render configuration
  selection/                Current stats-source query tooling
docs/                       Product, architecture, decisions, and validation
assets/placeholder/         Replaceable media placeholders
tests/                      Python regression and integration tests
```

Runtime state such as demos, databases, digests, render queues, and logs lives
under `state/` and is intentionally excluded from Git.

## Requirements

### Development and detection

- Python 3.12+
- Node.js 22+ for the Discord bot
- `pytest` for the Python test suite
- A match-discovery source that can emit the normalized game fields described
  above. Xerial's deployment uses a private Turso/libSQL database; it is not
  available as part of this repository.
- [MVD Analyzer upstream](https://github.com/galfthan/mvd_analyzer) for demo
  analysis. The current deployment uses
  [Xerialen's fork](https://github.com/Xerialen/mvd_analyzer) and consumes its
  `mvd-api` REST module.

### Rendering and assembly

The production rendering path additionally expects:

- Windows 11 with WSL2
- the dedicated `clipsmith-render` Ubuntu 22.04 WSL distro
- ezQuake and the required Quake content
- FFmpeg
- DaVinci Resolve with its Python scripting API enabled
- a local checkout of the external One More QUAD brand scenes

These integrations are configurable, but provisioning the complete render host
is currently documented rather than automated.

### MVD Analyzer integration

The canonical analysis project is
[galfthan/mvd_analyzer](https://github.com/galfthan/mvd_analyzer). The clipsmith
deployment currently uses
[Xerialen/mvd_analyzer](https://github.com/Xerialen/mvd_analyzer), a fork that
may contain integration-specific commits. MVD Analyzer owns MVD parsing,
analytics, result schemas, and consumer surfaces. clipsmith talks to its
`mvd-api` module over HTTP; it does not depend on a separate `mvd-api`
repository.

## Quick start for development

```bash
git clone https://github.com/Xerialen/clipsmith.git
cd clipsmith

python -m pip install pytest
python -m pytest

cd bot
npm ci
npm test
```

The test suites use temporary stores and mocked external clients; they do not
need production credentials.

Useful local commands:

```bash
# Inspect the candidate-store CLI
python -m clipsmith.store --help

# Build a weekly digest
python -m clipsmith.digest --help

# Preview the Discord bot without logging in
npm --prefix bot run dry-run

# Render a selected candidate on a configured render host
python -m clipsmith.render <candidate-id>

# Run the complete render + branding + Resolve pipeline
python scripts/produce_clip.py <candidate-id>
```

## Configuration

clipsmith never expects credentials in the repository. By default, live
services read `KEY=VALUE` entries from:

```text
~/.config/api-keys.env
```

Required credentials depend on the enabled stages. The two `TURSO_*` names are
specific to Xerial's current private discovery adapter; an alternative source
would define its own connection settings while preserving the normalized game
contract.

```dotenv
# Only for Xerial's private Turso/libSQL discovery source
TURSO_DB_URL=
TURSO_AUTH_TOKEN_QWSTATS=

# Only for the Discord review stage
CLIPSMITH_DISCORD_TOKEN=
CLIPSMITH_DISCORD_GUILD_ID=
CLIPSMITH_DISCORD_REVIEW_CHANNEL_ID=
```

Paths can be configured independently:

```dotenv
CLIPSMITH_OUTPUT_ROOT=
CLIPSMITH_STAGE_DIR=
CLIPSMITH_RAW_DIR=
CLIPSMITH_ASSETS_DIR=
CLIPSMITH_FINAL_DIR=
CLIPSMITH_MAP_SOURCE_DIR=
CLIPSMITH_BRAND_SCENES_DIR=
CLIPSMITH_BEATFORGE_DIR=
CLIPSMITH_WSL_STAGE_DIR=
```

See [`.env.example`](.env.example) for the complete list. The file is a
reference template; put real values in the external configuration file or the
service environment, not in a committed `.env` file.

Output defaults to `~/Videos/clipsmith` and is never written into the Git
repository.

## Design principles

- **Natural-language candidates:** every recommendation explains what happened,
  whose POV to watch, and why the moment ranked highly.
- **Human curation at the boundary:** detection and rendering are automated;
  editorial selection and final publication remain explicit decisions.
- **Rerunnable stages:** retries should converge without duplicate candidates,
  duplicate posts, or accidental overwrites.
- **One canonical store:** Python owns candidate schema and lifecycle rules;
  the Discord bot goes through the Python store CLI instead of maintaining a
  second implementation.
- **No committed runtime data:** demos, player-derived state, credentials, and
  generated media stay outside Git.

## Documentation

- [`docs/pipeline-spec.md`](docs/pipeline-spec.md) — product and pipeline contract
- [`docs/roadmap.md`](docs/roadmap.md) — phased delivery plan
- [`docs/decision-log.md`](docs/decision-log.md) — architectural decisions
- [`docs/testing-and-validation.md`](docs/testing-and-validation.md) — validation contract
- [`bot/README.md`](bot/README.md) — Discord bot details
- [`scripts/README.md`](scripts/README.md) — script inventory and render notes

## Security

Please report vulnerabilities privately through GitHub's **Security → Report a
vulnerability** flow. See [`SECURITY.md`](SECURITY.md). Never include live
credentials in an issue, log, fixture, or example.

## License

No license has been granted yet. Public visibility does not grant permission to
copy, modify, or redistribute this code.
