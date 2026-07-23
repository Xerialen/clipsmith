# bot/ — clipsmith Discord review bot

Phase-2 owner interface (issues #16–#18, #21): posts the Monday candidate
digest to the review channel and captures the owner's selections. Follows the
Evola-bot pattern (`~/projects/bots/Evola-bot`): discord.js v14, env from
`~/.config/api-keys.env`, timestamped logs, clean shutdown.

Issue #16 delivers the skeleton: config loading, an injectable
candidate-store layer, the startup health line, and `/clipsmith status`.
Issue #17 adds the Monday digest post (`src/post-digest.mjs`). Issue #18 adds
selection buttons, text fallback, and the render-queue handoff.

## Layout

| File | Role |
|---|---|
| `src/config.mjs` | Resolves `CLIPSMITH_DISCORD_TOKEN` / `_GUILD_ID` / `_REVIEW_CHANNEL_ID` from `~/.config/api-keys.env`. Values are never printed; errors name missing keys only. |
| `src/store.mjs` | Candidate-store access by shelling out to `python -m clipsmith.store` (the Python module stays the single source of truth for schema + status rules; no native SQLite dep). `exec` is injectable for tests. Reads via `list`; the single write path is `updateStatus` (`update-status`, lifecycle enforced by the CLI). |
| `src/health.mjs` | Startup health line: version, store reachability, counts by status. |
| `src/commands.mjs` | `/clipsmith status` definition + handler (plain API JSON, mock-testable). |
| `src/index.mjs` | Entrypoint: dry-run / live client wiring, slash-command registration, graceful shutdown. |
| `src/digest.mjs` | Digest rendering + posting logic (issue #17): loads the #11 digest JSON, builds one message per category ("`<CATEGORY> OF THE WEEK — candidates`") with one embed per candidate (description, POV, ranking reasoning, hub deep link), honors min-1/max-3 and the explicit empty-category message, guards all Discord embed limits, Europe/Stockholm week logic. Pure module, mock-testable. |
| `src/post-digest.mjs` | Digest-post CLI: `node src/post-digest.mjs [--digest <path>] [--channel <id>] [--dry-run]`. Default digest is `state/digests/<week>.json` for the current digest week (Europe/Stockholm; on Mondays that is the week that just finished, mirroring `clipsmith.digest`). After each successful category post it flips that message's candidates `candidate→posted`; a failed post never flips. `--dry-run` renders everything to stdout, sends nothing, writes nothing, needs no token, exits 0. |
| `src/selection.mjs` | Digest selection handling (issue #18): button custom IDs, `select <n>` text fallback, one-selection-per-category replacement semantics, selected-button visual state, and queue enqueue. Pure module, mock-testable. |
| `src/render-queue.mjs` | Durable Phase-2 → Phase-3 handoff queue. File-backed JSON, atomically rewritten, default `state/render-queue.json` or beside `CLIPSMITH_DB`; injectable path for tests. |

## Run locally

```powershell
cd bot
npm install
npm test          # node --test; mocked client + python-seeded temp store, no token needed
npm run dry-run   # prints the health line against the real store, exits 0 without a token
```

Environment knobs (all optional):

- `CLIPSMITH_ENV_PATH` — alternate env file (default `~/.config/api-keys.env`)
- `CLIPSMITH_DB` — alternate candidate DB (default `state/candidates.db`)
- `CLIPSMITH_RENDER_QUEUE` — alternate render queue JSON path (default
  `state/render-queue.json`, or next to `CLIPSMITH_DB` when set)
- `CLIPSMITH_PYTHON` — python executable for the store CLI (default `python`)

## Render queue contract

Selections emit a durable JSON handoff for Phase 3. Default path:
`state/render-queue.json` (override with `CLIPSMITH_RENDER_QUEUE`). When
`CLIPSMITH_DB` points at a temp or alternate store, the default queue lives
beside that DB as `render-queue.json`.

Schema:

```json
{
  "version": 1,
  "updated_at": "2026-06-10T21:30:00.000Z",
  "entries": [
    {
      "candidate_id": "abc123",
      "category": "QUAD RUNNER",
      "demo_sha": "...",
      "window": {"start_s": 100, "end_s": 135},
      "pov_player": {"name": "Milton", "userid": 35},
      "selected_at": "2026-06-10T21:30:00.000Z",
      "status": "pending"
    }
  ]
}
```

Phase 3 should poll `entries[]` where `status == "pending"`. Re-selecting a
different candidate in the same category marks any still-pending entry for that
category as `superseded` and appends a new `pending` entry, so consumers see at
most one pending render per category.

## Going live (after #12)

The owner creates the Discord server + bot application (issue #12) and stores
`CLIPSMITH_DISCORD_TOKEN`, `CLIPSMITH_DISCORD_GUILD_ID`,
`CLIPSMITH_DISCORD_REVIEW_CHANNEL_ID` in `~/.config/api-keys.env`. Then:

```powershell
cd bot
npm start
```

On connect the bot registers `/clipsmith` for the guild, posts the health line
to the review channel, answers `/clipsmith status` read-only against the
candidate store, handles digest selection buttons, and accepts `select <n>`
replies under digest category messages. Nothing else changes — the same code
runs in both modes.

The digest post (#17) is already buildable and fully dry-runnable today; #12
only unblocks the live send:

```powershell
# safe live test against a designated test channel first
node src/post-digest.mjs --channel <test-channel-id>
# the real Monday 20:00 invocation (review channel from the env file)
node src/post-digest.mjs
```

A live run posts one message per category to
`CLIPSMITH_DISCORD_REVIEW_CHANNEL_ID` (or the `--channel` override) and flips
the posted candidates `candidate→posted` in the store. Each candidate embed gets
a `Select as <CATEGORY> OF THE WEEK` button; the scheduled Monday-20:00
invocation (systemd timer, WSL side) lands in P2.4/#21 — until then the command
is run manually.
