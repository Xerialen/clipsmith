# scripts/branding/

The **branding/assembly stage** — turns a raw QW highlight capture (from
`scripts/render/`) into a branded clip: a fire-text intro that burns away to
reveal the capture framed in the Quad Runner overlay, with music.

Proven 2026-06-10 on render-host (the kippo-quad-run PoC). PoC: values are
hardcoded for that clip; the orchestrator will parameterize name/clan/colour
(from selection+analysis) and demo-specific timings. See `docs/testing-and-validation.md`
for evidence and the hard-won gotchas.

## Ownership boundary
This stage **consumes** brand assets owned by the **`Xerialen/onemorequad-brand`**
repo (the OBS scenes, fonts, colours) — it does not own them. `build.sh` points
at that repo's `Demo Clip Focus` scene and extracts the fonts from its bundle.
Finished clips go to **`C:\Users\example\Videos\clipsmith\branded\`** (never
in this repo — heavy media is gitignored and the output convention is Drive).

## Pipeline
```
build.sh        # produce renderable inputs (fonts, intro frames, overlay)
  └─ assemble_branded.sh   # SFX + framed clip + audio bed + final composite
```

## Node helpers (run via the two .sh drivers; need `playwright` + `pngjs`)
- `pick_fonts2.mjs <scene.html> <outdir>` — extract brand fonts from a scene's
  bundle manifest. **Gotcha:** `Nightrunner` (the wordmark font) is stored
  **gzip-compressed TrueType** (`compressed:true`, magic `00 01 00 00`), NOT
  woff2 like the others — gunzip then embed as `data:font/ttf … format('truetype')`.
- `render_template.mjs <scene.html> <outdir>` — render an OBS scene to `[flat]`
  + `[alpha]` PNGs (waits for the bundler to mount, forces transparent bg,
  `omitBackground`). The scene's video region is a genuine alpha hole.
- `holes_check.mjs` / `bbox_full.mjs` — render a scene over magenta / measure the
  transparent hole bbox (used to confirm a scene is cam-free and find the fill box).
- `build_intro.mjs <outdir>` — emit `intro.html`: frame-driven doom-fire +
  "KIPPO [FU]" Nightrunner slam (shockwave + flash) + flames + the last ~0.75s
  burning the frame to transparent (alpha erase mask). Fonts inlined as base64.
- `render_frames.mjs <outdir> <count> [only]` — step the intro animation and
  screenshot each frame with alpha → `intro_frames/fNNN.png`.
- `build_lowerthird.mjs` / `render_one.mjs` — the corrected "KIPPO [FU] · DM3"
  card (the baked scene text says "ZAMSHA · DM3" — encoded in the bundle, so we
  cover it). **Gotcha:** canvas does NOT lazy-load `@font-face` like the DOM —
  `await document.fonts.load(...)` (try/catch) before `fillText`, else fallback.

## Bash drivers
- `build.sh` — runs the node steps; outputs `intro_frames/`, `lowerthird.png`,
  `frame_overlay_kippo.png`.
- `assemble_branded.sh` — synth SFX (impact boom + fire whoosh, no sourcing),
  build the framed clip, mix the audio bed (beatforge music over, game audio low),
  composite: black ← clip starting at the reveal ← intro alpha burning away on top.

## Variants
Two finished versions of the PoC exist (see decision-log): **v3 fire-burn** (this
pipeline — flames erode the intro to reveal the clip; matches the brief) and a
**v2 zoom-reveal** built via the Resolve MCP. v3 is the canonical scripted path.
