#!/bin/bash
# Assemble 3 POV captures into per-POV 75%-speed MP4s + one 3-wide composite.
# POVs: cap_pov2 (zamsha/quad), cap_pov3 (realpit/ring), cap_pov4 (ok98/pent)
set -eu
cd /root

for P in 2 3 4; do
  D=/root/cap_pov${P}
  test -d "$D" || { echo "missing $D"; exit 1; }
  # 75% speed: video setpts /0.75, audio atempo=0.75
  ffmpeg -y -loglevel error -framerate 30 -i ${D}/shot-%06d.png -i ${D}/audio.wav \
    -filter_complex "[0:v]setpts=PTS/0.75,format=yuv420p[v];[1:a]atempo=0.75[a]" \
    -map "[v]" -map "[a]" -r 30 -c:v libx264 -crf 20 -c:a aac \
    /root/pov${P}_75.mp4
  echo "pov${P}_75.mp4 done"
done

# 3-wide composite: scale each to 854x480 -> 2562x480, zamsha audio
ffmpeg -y -loglevel error \
  -i /root/pov2_75.mp4 -i /root/pov3_75.mp4 -i /root/pov4_75.mp4 \
  -filter_complex "[0:v]scale=854:480[a];[1:v]scale=854:480[b];[2:v]scale=854:480[c];[a][b][c]hstack=inputs=3[v]" \
  -map "[v]" -map 0:a -c:v libx264 -crf 20 -c:a copy \
  /root/clipsmith_hx_push_3pov_75.mp4

# Stage on the host-mounted filesystem. Set CLIPSMITH_WSL_STAGE_DIR to a path
# visible from this distro, for example /mnt/c/clipsmith/stage.
STAGE_DIR="${CLIPSMITH_WSL_STAGE_DIR:-}"
if [ -z "$STAGE_DIR" ]; then
  echo "error: set CLIPSMITH_WSL_STAGE_DIR" >&2
  exit 2
fi
mkdir -p "$STAGE_DIR"
cp /root/pov2_75.mp4 /root/pov3_75.mp4 /root/pov4_75.mp4 \
  /root/clipsmith_hx_push_3pov_75.mp4 "$STAGE_DIR/"
ls -la "$STAGE_DIR/"
