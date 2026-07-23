#!/usr/bin/env bash
# clipsmith branding — build the renderable inputs the assembler needs:
#   brand fonts (extracted from the OneMoreQUAD bundle), the fire-text intro
#   frame sequence (alpha PNGs), and the framed-clip overlay (Quad Runner
#   "Demo Clip Focus" + corrected KIPPO lower-third).
#
# Then run ./assemble_branded.sh to produce the final MP4.
# Requires: node + `npm i playwright pngjs` (Chromium installed), ffmpeg.
set -euo pipefail
WORK="${WORK:-$(pwd)}"
# Brand source: the OneMoreQUAD brand repo's OBS scenes.
BRAND="${BRAND:-${CLIPSMITH_BRAND_SCENES_DIR:-}}"
if [ -z "$BRAND" ]; then
  echo "error: set BRAND or CLIPSMITH_BRAND_SCENES_DIR" >&2
  exit 2
fi
SCENE_FOCUS="${SCENE_FOCUS:-QUAD RUNNER - Demo Clip Focus (1920x1080).html}"
# Any scene file carries the full brand font set in its bundle manifest:
FONT_SRC="${FONT_SRC:-QUAD RUNNER - Starting Soon (1920x1080).html}"
cd "$WORK"

echo "[1] extract brand fonts from the OneMoreQUAD bundle (Nightrunner is gzip'd TTF!)"
node pick_fonts2.mjs "$BRAND/$FONT_SRC" "$WORK"

echo "[2] build + render the fire-text intro (150 alpha frames, ~55s)"
node build_intro.mjs "$WORK"
rm -rf intro_frames && mkdir -p intro_frames
node render_frames.mjs "$WORK" 150

echo "[3] build the corrected KIPPO lower-third card"
node build_lowerthird.mjs "$WORK"
node render_one.mjs "$WORK/lowerthird.html" "$WORK/lowerthird.png"

echo "[4] render the Demo Clip Focus frame overlay (alpha) and composite the lower-third"
node render_template.mjs "$BRAND/$SCENE_FOCUS" "$WORK"
ffmpeg -y -i "QUAD RUNNER - Demo Clip Focus (1920x1080) [alpha].png" -i lowerthird.png \
  -filter_complex "[0][1]overlay" -frames:v 1 frame_overlay_kippo.png

echo "done. inputs ready in $WORK — now run: ./assemble_branded.sh"
