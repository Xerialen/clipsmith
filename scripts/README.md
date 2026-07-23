# scripts/

PoC scripts, proven 2026-06-09/10 (see `docs/testing-and-validation.md` for evidence).
These ran from scratch dirs originally; this repo is now canonical. Runtime
paths are configured through the `CLIPSMITH_*` variables documented in the
root `.env.example`; credentials remain outside the repository.

## selection/
- `turso_query.py` — query CLI for Xerial's private stats database, currently
  hosted on Turso/libSQL (`--tables`, `--schema <t>`, or a SQL string). It is
  the current discovery adapter, not a public data service bundled with
  clipsmith. Reads `TURSO_DB_URL` + `TURSO_AUTH_TOKEN_QWSTATS` from
  `~/.config/api-keys.env`; never exposes the token to the shell. Run inside
  WSL2 Ubuntu-24.04.

## analysis/
Run against a demo loaded into mvd-api (WSL2 `:8080`,
`~/mvd-mcp-bundle/mvd-api`). Demo sha is currently hardcoded per script (PoC).
- `rl_kill_timeline.py` — frag-stream filter: kills BY one team WITH the RL.
- `rl_carrier_kills.py` — kills OF enemy RL carriers (joins
  `getBackpacks(weapon=rl)` drops to the frag stream by victim+time).
- `window_detail.py` — blow-by-blow (frags, powerups, pack drops, chat) for a
  time window; used to verify a candidate highlight before rendering.

## render/
The render orchestrator (issue #19/#53): one command turns a candidate id into
a raw capture on Drive, fully hands-off. All verified engine gotchas (classic GL, trigger→exec seek escape, autotrack clearing,
track-after-seek, console suppression, ext4 basedir staging, frame validation)
are encoded in `clipsmith.render` + `render_worker.sh` — see the module
docstring for the full list with engine-source citations.
- `python -m clipsmith.render <candidate-id>` — canonical Windows-side
  entrypoint. Reads demo_sha/map/pov userid/window from the candidate store,
  fetches the demo from the d.quake.world CDN into `state/demos/` if missing,
  generates the cfg pair from parameters, drives the capture in the
  `clipsmith-render` WSL distro, moves the .mov to
  `CLIPSMITH_RAW_DIR` and flips the candidate's status to `rendered`.
  Requires status `selected`; `--force-status` for
  ad-hoc renders.
- `render_candidate.py <candidate-id>` — compatibility wrapper around
  `python -m clipsmith.render` for old commands.
- `render_worker.sh` — distro-side worker (invoked by the orchestrator via
  `wsl -d clipsmith-render`, never run on the main distro): stages demo+cfgs
  into the ext4 basedir `/root/nquake`, runs Xvfb + the extracted stock
  ezQuake AppImage with `+set vid_renderer 0`, early-validates frame ~200
  (or a clamped frame for short validation captures), assembles PNG seq +
  audio.wav into the locked .mov intermediate and copies it to
  `CLIPSMITH_STAGE_DIR`.
- `assemble_3pov.sh` — per-POV 75%-speed MP4s + 3-wide hstack composite;
  stages to the required `CLIPSMITH_WSL_STAGE_DIR`. Still the only multi-POV
  recipe (PoC).
- Retired 2026-06-10/11 (superseded by the orchestrator): old PoC capture
  scripts and cfg pairs. The proven cfg pair pattern now lives in
  `clipsmith.render.build_arm_cfg` / `build_capgo_cfg`.
