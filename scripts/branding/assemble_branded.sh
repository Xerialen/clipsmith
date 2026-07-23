#!/usr/bin/env bash
# clipsmith — branding/assembly stage (the Phase-1 "branded MP4 end-to-end" seam)
#
# Takes a rendered QW highlight (the raw capture) and produces a branded clip:
#   5s fire-text intro ("KIPPO [FU]" slam + explosion) --burns away--> the capture
#   framed in the Quad Runner "Demo Clip Focus" overlay, music throughout.
#
# Built/verified 2026-06-10 on render-host (the kippo-quad-run PoC clip).
# PoC: values below are hardcoded for that clip. The orchestrator will parameterize
# (name/clan/colour from selection+analysis, demo-specific timings). See README.md.
#
# Runs on the Windows side (Git-Bash/WSL with ffmpeg + node + Playwright Chromium).
# Audio is SYNTHESIZED (no sourcing); music is a beatforge MusicGen beat (own, no licensing).
set -euo pipefail

WORK="${WORK:-$(pwd)}"                  # work dir holding the *.mjs + intro_frames/ + fonts
CLIP="${CLIP:-G:/My Drive/captures/quake/highlights/raw/2026-06-08_dm3_fu-vs-mix_kippo-quad-run_1pov_poc.mp4}"
OVERLAY="${OVERLAY:-frame_overlay_kippo.png}"   # Demo Clip Focus alpha + corrected KIPPO lower-third (built by build.sh)
BEAT="${BEAT:-G:/My Drive/projects/pinnacle/beatforge/output/beat_1773694461.wav}"
OUT="${OUT:-kippo_fire_burn_final.mp4}"
REVEAL="${REVEAL:-4.27}"                 # when the burn starts / clip is revealed (intro frame 128 @30fps)
TOTAL="${TOTAL:-34.30}"                  # intro reveal window + 30s clip
cd "$WORK"

echo "[1/4] synth SFX (impact boom + fire whoosh)"
ffmpeg -y -f lavfi -i "sine=frequency=58:duration=0.5" -f lavfi -i "sine=frequency=86:duration=0.5" \
  -f lavfi -i "anoisesrc=d=0.18:color=brown:amplitude=0.7" \
  -filter_complex "[0]volume=1.0[a];[1]volume=0.6[b];[2]lowpass=f=900,volume=1.2[c];\
[a][b][c]amix=inputs=3:normalize=0,afade=t=out:st=0.04:d=0.46:curve=exp,volume=4.0,alimiter=limit=0.95" \
  -ar 48000 -ac 2 impact.wav
ffmpeg -y -f lavfi -i "anoisesrc=d=0.75:color=white:amplitude=0.8" \
  -af "highpass=f=350,lowpass=f=6000,aphaser=in_gain=0.6:out_gain=0.8:delay=3:decay=0.5:speed=1.5,\
afade=t=in:st=0:d=0.4:curve=exp,afade=t=out:st=0.45:d=0.3,volume=2.2,alimiter=limit=0.95" \
  -ar 48000 -ac 2 whoosh.wav

echo "[2/4] framed clip (capture scaled to 1080p + Quad Runner overlay on top)"
ffmpeg -y -i "$CLIP" -i "$OVERLAY" \
  -filter_complex "[0:v]scale=1920:1080:flags=lanczos[s];[s][1:v]overlay=0:0[v]" \
  -map "[v]" -map 0:a -c:v libx264 -crf 18 -preset fast -c:a aac -b:a 192k framed_clip.mp4

echo "[3/4] audio bed (beatforge music over, game audio low, impact@slam, whoosh@burn)"
ffmpeg -y -i "$BEAT" -i impact.wav -i whoosh.wav -i framed_clip.mp4 -filter_complex "\
[0:a]atrim=0:${TOTAL},asetpts=PTS-STARTPTS,volume=0.95,afade=t=out:st=31.8:d=2.5[m];\
[1:a]adelay=333|333,volume=1.0[i];\
[2:a]adelay=4270|4270,volume=0.9[w];\
[3:a]adelay=4270|4270,volume=0.22[g];\
[m][i][w][g]amix=inputs=4:normalize=0:dropout_transition=0,alimiter=limit=0.97,aresample=48000[a]" \
  -map "[a]" -ac 2 -ar 48000 full_audio.wav

echo "[4/4] final composite (black base <- clip@reveal <- intro alpha burning away on top)"
# The intro PNG seq carries alpha; its last ~0.75s erodes to transparent (fire-mask
# destination-out, in build_intro.mjs), revealing the clip that starts at $REVEAL.
ffmpeg -y \
  -framerate 30 -i "intro_frames/f%03d.png" \
  -itsoffset "$REVEAL" -i framed_clip.mp4 \
  -i full_audio.wav \
  -filter_complex "\
color=black:s=1920x1080:r=30:d=$(echo "$TOTAL+0.01" | bc)[bg];\
[bg][1:v]overlay=0:0:eof_action=pass[base];\
[base][0:v]overlay=0:0:eof_action=pass,format=yuv420p[v]" \
  -map "[v]" -map 2:a -t "$TOTAL" \
  -c:v libx264 -crf 19 -preset medium -pix_fmt yuv420p -c:a aac -b:a 256k "$OUT"

echo "done -> $WORK/$OUT"
echo "stage to: G:/My Drive/captures/quake/highlights/branded/  (per output convention)"
