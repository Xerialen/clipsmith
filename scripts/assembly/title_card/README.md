# scripts/assembly/title_card/

The **OF THE WEEK title-card generator** (issue #23, pipeline-spec Assembly
item 2): `"<CATEGORY> OF THE WEEK"` + week number, player name beneath, with
category styling — QUAD RUNNER blue-glow name pulsating **once** in sync with
the quad pickup sound, PENT RUNNER the same in pentagram red (+ pentagram
texture), everything else (TEAM KILLER, PENT GRIND, …) plain with a silent stem.

```
python scripts/assembly/title_card/make_title_card.py \
    --category "QUAD RUNNER" --player "kippo (FU)" --week 24
```

## Output (format decision)

`C:\Users\example\Videos\clipsmith\assets\generated\weekNN_<cat>_<player>.mp4`
— 1920x1080 30 fps H.264 yuv420p + AAC, 3.5 s — plus the bare `.wav` audio stem
beside it. **Why MP4-with-audio:** it imports into DaVinci Resolve as a single
media-pool clip with the audio already attached; a PNG sequence imports as a
video-only frames-clip and needs manual audio conform. The separate WAV stem
lets the Resolve assembly (#25) re-balance the pickup sound against the music
bed if needed.

## Pulse <-> sound sync (one timing source)

`PULSE_PEAK_S = 1.2 s` in `make_title_card.py` drives **both** the animation
(pulse peak at frame `1.2*fps = 36`) and the audio (`adelay` places the sound's
transient at exactly 1.2 s). Verified: `silencedetect` on the stem shows the
sound starting at ~1.200 s and the frame extracted at 1.2 s is the pulse maximum.

## Sounds — HITL #14 drop-in point

`sound_config.json` maps category → `{path, transient_offset_s}`. The shipped
`sounds/PLACEHOLDER_*.wav` are ffmpeg-synthesized stand-ins (`synth_sounds.py`,
same layered-sines + noise-impact pattern as `scripts/branding/assemble_branded.sh`)
with their attack at t=0. When #14 delivers the real quad/pent pickup sounds,
point `path` at them and set `transient_offset_s` to where the attack sits in
the file — the pulse stays locked, **no code change**.

## Files

- `make_title_card.py` — CLI + orchestration (render → stem → mux)
- `card_html.py` — frame-driven HTML/canvas card (`window.step()`, deterministic)
- `brand_fonts.py` — extracts brand fonts from the `Xerialen/onemorequad-brand`
  scene bundle (consumed in place, cached to `%LOCALAPPDATA%\clipsmith\brand_fonts`)
- `synth_sounds.py` + `sounds/` — placeholder pickup SFX (<200 KB, tracked)
- `sound_config.json` — the #14 drop-in point

## Branding and rendering gotchas

- **Nightrunner is gzip-compressed TrueType** in the bundle manifest — gunzip,
  embed as `format('truetype')`; raw-as-woff2 fails silently to serif.
- **Canvas does not lazy-load `@font-face`** — every face/size is
  `document.fonts.load(...)`-ed before the first draw (`window.__ready` gate).
- Rendering is **frame-driven** Playwright screenshots (python-playwright using
  the installed ms-playwright Chromium via `executable_path`), so the pulse
  peak is an exact frame, not a wall-clock approximation.
