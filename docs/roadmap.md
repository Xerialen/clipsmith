# Roadmap — OF THE WEEK pipeline

last-verified: 2026-06-10

The proof of concept established every major technical seam on real clips. The
phases below turn those seams into the production pipeline defined in
[pipeline-spec.md](pipeline-spec.md). Each phase has an observable outcome and
can be delivered incrementally.

## Phase 1 — Detection & candidates (`phase:1-detection`)

The brain. New-demo watcher over a replaceable match-discovery source (Xerial's
private Turso/libSQL stats database in the current deployment), four detectors
(QUAD RUNNER,
TEAM KILLER, PENT GRIND, PENT RUNNER) emitting into a shared candidate store,
hub deep-link resolution, human-readable descriptions, weekly digest builder.
**Demo:** run the CLI against last week's demos and get a correct, ranked
candidate digest as JSON.

## Phase 2 — Discord review loop (`phase:2-discord`)

The owner interface. Bot + dedicated server (HITL), Monday-20:00 digest post
with per-candidate embeds + hub URLs, selection capture via buttons, the
3×/week + Monday scheduling deployed as systemd timers.
**Demo:** a real Monday digest lands in Discord; clicking a button flips the
candidate to `selected`.

## Phase 3 — Render & assembly (`phase:3-render`)

The factory. Parameterized render orchestrator (sha+window+POV → capture),
hub-parity capture config incl. QTV-style teaminfo (research), capture-quality
upgrade matched to Resolve + YouTube settings, title cards with
category-specific pulse+sound, kill-counter overlay, Resolve MCP assembly
(intro → title → framed clip → outro), one full tracer-bullet render.
**Demo:** a selected candidate renders to a finished branded MP4 hands-off.

## Phase 4 — Publish & feedback (`phase:4-publish`)

The mouth. Metadata generator (hashtags, description with community links),
browser-automation upload to the PLAY OF THE WEEK playlist (unlisted),
review notification, 👍/"OK" approval → public, feedback-thread revision loop.
**Demo:** one clip goes candidate → Discord → render → unlisted upload →
approval → public, end to end.

## Phase 5 — Comeback & hardening (`phase:5-hardening`)

The long haul. COME-BACK OF THE WEEK detector (score-trend reconstruction),
failure alerts to Discord, idempotency/watermark hardening so unattended runs
are safe, lightweight engagement metrics.
**Demo:** the pipeline survives a full month unattended; comeback candidates
appear in the digest.

## Sequencing notes

- Phases 1→2 are strictly ordered (digest needs candidates).
- Phase 3 can start in parallel with Phase 2 (render needs only the candidate
  store schema from Phase 1).
- Phase 4 needs Phases 2+3. Phase 5 is independent backlog after 1.
- Quality bar: deterministic tests plus a smoke test per stage; non-trivial
  pull requests receive independent review.
