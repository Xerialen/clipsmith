# Picture settings lock — capture → Resolve → YouTube (issue #22)

last-verified: 2026-06-11
status: **LOCKED** — all rows. The issue-22 benchmark (6 runs, table below)
landed 2026-06-11; capture locked at **1920×1080@60, default adapter**, and
the timeline fps follows at 60. Consume freely (#25).

The end-to-end principle: **one resolution, one frame rate, no retiming, no
scaling** from engine capture to the Resolve timeline; quality is only
deliberately reduced once, at YouTube delivery. Sources: vault
`podcasting/channel-art-specs.md` (YouTube encoding targets, verified
2026-05-31), clipsmith validation results, the decision log, and the issue-22 benchmark.

## The locked chain

| Stage | Setting | Value | Status |
|---|---|---|---|
| **Capture** (ezQuake `demo_capture`) | resolution | 1920×1080 | LOCKED |
| | fps (`demo_capture_fps`) | 60 | LOCKED |
| | GPU adapter | **default** (no `MESA_D3D12_DEFAULT_ADAPTER_NAME` wiring — the benchmark showed the adapter is irrelevant, see below) | LOCKED |
| | HUD coordinate space | `vid_conwidth 1280` / `vid_conheight 720` at 1080p (parity_hud.cfg element scales were tuned at 720p; locking the 2D console space to 720p preserves the layout at any output size) | LOCKED — visually verified on bench frame 250 (1080p60) |
| **Intermediate** (PNG+wav → raw clip on Drive) | container | **.mov** (was .mp4 — changed so audio can stay PCM) | LOCKED |
| | video codec | H.264 (libx264) **CRF 14, preset slow**, `yuv420p`, GOP = fps (1 s keyframes for snappy Resolve scrubbing) | LOCKED |
| | audio codec | **PCM s16le 48 kHz** (lossless; the wav is the only audio generation before Resolve — don't spend it on AAC twice) | LOCKED |
| **Resolve timeline** (#25 consumes this) | resolution | 1920×1080 | LOCKED |
| | frame rate | **60.000** (= capture fps; read it from the clip — never let Resolve retime) | LOCKED |
| | color science | DaVinci YRGB, Rec.709 (Scene) / Gamma 2.4, no input LUT | LOCKED |
| | scaling | none for the gameplay clip on a matching timeline; branded-frame inserts scale to fit, never crop gameplay (pipeline-spec rule) | LOCKED |
| **Resolve delivery** (YouTube master) | format / codec | MP4, H.264 **High profile**, CABAC | LOCKED |
| | resolution / fps | 1920×1080 @ timeline fps | LOCKED |
| | bitrate | **12,000 kbps** at 1080p60 — vault YouTube SDR upload table | LOCKED |
| | GOP | keyframe interval = **half the frame rate** (30 frames @60fps), **closed GOP**, 2 consecutive B-frames (YouTube upload recommendation) | LOCKED |
| | audio | **AAC-LC 384 kbps, 48 kHz stereo** (vault YouTube row) | LOCKED |

## Why these values

### Capture 1920×1080@60 (locked)

- YouTube serves 1080p as its own tier; a 720p upload caps the viewer at 720p
  forever. 1080p is the channel-art-specs canvas for the OneMoreQUAD brand
  assets (`Demo Clip Focus` frame is 1920×1080), so a 1080p capture drops into
  the branded frame 1:1 instead of the PoC's 1.5× upscale.
- QuakeWorld is a 1996-asset game — resolution beyond 1080p buys almost
  nothing visually, and the branded frame is authored at 1080p. 1440p/4K
  rejected.
- 60 fps: QW is a high-speed movement game; 30 fps visibly stutters on fast
  flicks and rocket fights. The hub player and every modern QW stream run at
  60+. The only argument against was render cost — the benchmark settled it:
  ~11.5 min wall per 35 s clip at 1080p60 (622 s capture + 73 s assembly),
  inside the ~15 min/clip a weekly cadence affords. Locked at 60.
- The fps must be decided HERE because everything downstream inherits it:
  a 30 fps capture on a 60 fps timeline means retiming (vetoed), and a 60 fps
  timeline delivered at 30 throws away the benefit.

### Benchmark (capture throughput, kippo demo dm3, 35 s window, parity HUD)

Ran 2026-06-11 in the clipsmith-render distro (lead-verified). Six runs:
three modes × two adapters (default = AMD Radeon iGPU d3d12 vs
`MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA` = RTX 4090; renderer proof read
from the engine's GL vendor string each run).

| Mode | Adapter (renderer proof) | Frames | Startup (s) | Throughput (fps) | 35 s capture (s) | Assembly (s) | Size (MB) |
|---|---|---|---|---|---|---|---|
| 1280×720@30 | default | 301 | 19.3 | 7.46 | 141 | 20 | 85 |
| 1280×720@30 | nvidia — D3D12 (NVIDIA GeForce RTX 4090) | 301 | 19.2 | 7.50 | 140 | 25 | 85 |
| 1920×1080@30 | default — D3D12 (AMD Radeon(TM) Graphics) | 301 | 19.5 | 3.33 | 316 | 44 | 194 |
| 1920×1080@30 | nvidia — D3D12 (NVIDIA GeForce RTX 4090) | 301 | 19.4 | 3.40 | 309 | 45 | 196 |
| 1920×1080@60 | default — D3D12 (AMD Radeon(TM) Graphics) | 602 | 20.2 | 3.38 | 622 | 73 | 237 |
| 1920×1080@60 | nvidia — D3D12 (NVIDIA GeForce RTX 4090) | 602 | 20.4 | 3.36 | 625 | 74 | 237 |

**Adapter finding — the GPU is irrelevant.** Throughput is identical on the
RTX 4090 and the AMD iGPU at every mode (7.46 vs 7.50, 3.33 vs 3.40, 3.38 vs
3.36 fps): the bottleneck is the PNG capture path (per-frame PNG encode +
write), not 3D rendering — exactly what you'd expect for 1996 geometry. This
**closes the decision-log `MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA` revisit**:
no env-var wiring in the worker, capture runs on the default adapter.

Two other reads from the table:

- Throughput is per-PNG-frame, so 1080p60 costs exactly 2× 1080p30 wall time
  (same ~3.3 fps, twice the frames) — the fps choice is a pure time/quality
  trade with no efficiency cliff.
- **Drive cost:** a 35 s raw clip at the locked settings is ~237 MB. Fine at
  a weekly cadence, but raw-clip pruning after publish is a **Phase 5
  concern** (the raw dir is an ingest cache, not an archive).

### Intermediate: H.264 CRF 14 .mov + PCM (not DNxHR / qtrle / CRF 20)

The raw clip is an **ingest master for Resolve**, not a delivery file — but it
also lives on Google Drive, so size matters:

- **DNxHR HQ** at 1080p60 is ~700+ MB per 35 s clip (≈160 Mbps). qtrle even
  bigger. Rejected: Drive cost for no visible gain over high-quality H.264 on
  this content, and Resolve (Windows, Studio) decodes H.264 fine.
- **CRF 20 medium (the PoC)** shows mild blocking on quad-flash explosions
  and sky gradients; it was chosen for speed, not quality.
- **CRF 14 slow** is visually transparent on QW content, encodes well within
  budget (73 s assembly vs 622 s capture at 1080p60 — x264 on the 7800X3D is
  not the bottleneck), and lands at **~237 MB per 35 s clip at 1080p60**
  (benchmark table above; 720p30 was 85 MB). GOP = 1 s keeps Resolve
  scrubbing responsive (long-GOP H.264 scrubs poorly).
- **Audio goes PCM** (.mov container required — mp4+pcm is a compatibility
  trap): the engine hands us a lossless wav; encoding it to AAC here AND at
  delivery is two lossy generations for zero benefit. PCM stereo 48 kHz costs
  ~6.7 MB per 35 s. **#25 note: raw files are now `*_1pov_raw.mov`.**

### Resolve timeline

Match the clip exactly: 1920×1080, fps = capture fps. Resolve's default
behavior when clip fps ≠ timeline fps is frame-blend/retime — that's the
mismatch this issue exists to kill (the 2026-06-10 branded PoC was a 1080p30
assembly of a 720p30 capture: scaled AND mismatched against the 60 fps
target). Color: the capture is sRGB-ish game output with no camera log;
DaVinci YRGB + Rec.709 (Scene)/Gamma 2.4 passes it through untouched —
no input transform, no LUT.

### Resolve delivery → YouTube

Straight from the vault YouTube table (channel-art-specs, verified
2026-05-31):

- SDR upload bitrate: **1080p 8 Mbps, 1080p60 12 Mbps** → we deliver VBR
  ~12 Mbps (restrict max ≈ 24 Mbps) at 60 fps. This is the *recommended
  upload* rate — going far above it only slows upload; going below it starves
  YouTube's transcoder.
- **AAC-LC 384 kbps 48 kHz stereo** — YouTube's stated audio target.
- GOP per YouTube's upload recommendations: **closed GOP, keyframe interval
  half the frame rate, 2 consecutive B-frames, CABAC, High profile**.
  In Resolve: Restrict keyframes to every 30 frames + "use closed GOP" /
  B-frames 2 on the H.264 encoder panel.
- Container MP4 (vault row), moov-faststart irrelevant for uploads but
  harmless.

### YouTube's re-encode reality (why the headroom matters)

YouTube re-encodes everything. At 1080p, low-view videos get the **AVC
(H.264) ladder at ~2–4 Mbps**; the better **VP9** encodes are typically only
produced once a channel/video has decent traction. Consequences we accept and
plan for:

- Feed the transcoder the best plausible source: 12 Mbps upload means the
  inevitable ~3 Mbps AVC version degrades from a clean master, not from an
  already-starved one. QW's confetti-particle explosions + flat walls are
  exactly the content that shows transcoder starvation as smearing.
- Known lever, deliberately NOT adopted now: uploading at 1440p+ forces the
  VP9 ladder immediately ("1440p trick"). Rejected for launch — it upscales
  1996 content 1.78× for an encoder-policy hack and bloats render/upload
  time; revisit if published clips look visibly muddy at 1080p.

## Orchestrator wiring (issue #22 deliverable)

`clipsmith.render` defaults become the locked capture settings
(`--width/--height/--fps` stay overridable); the compatibility command
`scripts/render/render_candidate.py` delegates to that module. The parity HUD
chain is exec'd by the generated arm cfg with the 720p console-space lock at
1080p; `scripts/render/render_worker.sh` assembles with the locked intermediate
settings. Early-validation (frame #200 signalstats) and blackdetect gates
unchanged — thresholds are resolution-independent (YAVG/SATAVG are averages).

## Validation run (issue #22 definition of done)

DONE 2026-06-11 — one real candidate through the orchestrator at the locked
defaults (no overrides beyond display/staging isolation):

    python -m clipsmith.render e3bf28449a814e0390af11df01374fa8 \
        --display 97 --staging-root /root/render-97

- **Candidate:** `e3bf2844` QUAD RUNNER — Hmr on dm2, score 12.5 (top scorer
  in store), fresh 2026-06-10 demo (`5b2847141567…`), window
  1095.19–1130.19 s, walked candidate→posted→selected before the run.
- **Wall clock:** 18 m 10 s start-to-Drive (07:09:20 → 07:27:30), of which
  capture ≈ 12 min (2462 PNG frames), assembly + blackdetect + the 342 MB
  move to Drive the rest. A 35 s window costs ~41 s of capture (preroll +
  ceil), so real clips run a couple of minutes over the bench's bare-35 s
  number — still inside the ~15–20 min/clip weekly budget.
- **Early validation:** frame #200 YAVG=41.3 (pass); SATAVG=0.52 tripped the
  low-saturation *warning* (dm2's grey/dark palette, exactly the calibrated
  false-positive case) — visually verified textured, not the GREY render.
- **blackdetect:** none (clean).
- **Duration:** ffprobe 41.033 s == plan capture_s 41 = ceil(35 s window +
  5 s preroll + 0.19 s fractional window end); 2462 frames (2460 planned + 2
  in the kill-grace window).
- **ffprobe:** `h264, 1920x1080, 60/1` + `pcm_s16le, 48000 Hz, 2 ch` in .mov
  — the locked intermediate, confirmed.
- **HUD/POV frames:** t=15 s and t=30 s extracted and reviewed — parity HUD
  (clock, teaminfo rows, team overlay, status bar) legible at the locked
  720p-console-space scale; quad-glow on the view weapon confirms the right
  POV. Bench frame #250 (1080p60 run) separately confirmed the conlock scale.
- **Output:** `C:\Users\example\Videos\clipsmith\raw\2026-06-10_dm2_fu-vs-red_hmr-quad-runner_1pov_raw.mov`,
  342.2 MB (41 s of busy dm2 action — above the bench's 237 MB/35 s, scales
  with content); store status flipped `selected -> rendered`.

## See also

- vault `podcasting/channel-art-specs.md` — YouTube encoding table (source of
  the delivery numbers; do not duplicate-edit there)
- `docs/testing-and-validation.md` 2026-06-09/10/11 — render gotchas the worker encodes
- `docs/decision-log.md` — render-host decision + the
  `MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA` revisit note this issue executes
- issue #25 — Resolve assembly automation (consumes the timeline + delivery
  rows above)
