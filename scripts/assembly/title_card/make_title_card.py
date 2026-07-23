"""clipsmith title-card generator (issue #23, pipeline-spec Assembly item 2).

Produces the "<CATEGORY> OF THE WEEK" opening card as a ~3.5 s 1920x1080 30 fps
**MP4 (H.264 yuv420p + AAC)** plus the bare **WAV audio stem** next to it.

Output-format choice (documented per ticket): a self-contained MP4 with audio
imports into DaVinci Resolve as one media-pool clip (drag once, audio attached)
— PNG sequences import too but arrive as a frames-clip without audio and need
manual conforming. The separate WAV stem is also written so the Resolve
assembly (#25) can re-balance the pickup sound against the music bed if wanted.

Category styling (pipeline-spec):
  - QUAD RUNNER  -> player name in quad-blue glow, pulsating ONCE in sync with
                    the quad pickup sound
  - PENT RUNNER  -> same treatment in pentagram red + pentagram texture
  - anything else (TEAM KILLER, PENT GRIND, ...) -> plain name, no pulse,
                    silent audio stem

Pulse/sound sync: one timing source. The pulse peak is a fixed frame
(PULSE_PEAK_S * fps); the pickup sound is delayed so its transient
(sound_config.json: transient_offset_s into the file) lands at exactly
PULSE_PEAK_S. Real game sounds from HITL #14 drop in via sound_config.json —
no code change.

Rendering: Playwright HTML/canvas, frame-driven (window.step()), the technique
proven in scripts/branding/ (2026-06-10) — including the gzip'd-Nightrunner and
document.fonts.load gotchas (see brand_fonts.py / card_html.py).

Usage:
  python scripts/assembly/title_card/make_title_card.py \
      --category "QUAD RUNNER" --player "kippo (FU)" --week 24
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from brand_fonts import extract_fonts, font_face_css  # noqa: E402
from card_html import build_card_html  # noqa: E402

OUTPUT_ROOT = Path(
    os.environ.get("CLIPSMITH_OUTPUT_ROOT", Path.home() / "Videos" / "clipsmith")
)
DEFAULT_OUT_DIR = Path(
    os.environ.get("CLIPSMITH_ASSETS_DIR", OUTPUT_ROOT / "assets" / "generated")
)
SOUND_CONFIG = SCRIPT_DIR / "sound_config.json"

FPS = 30
DURATION_S = 3.5
PULSE_PEAK_S = 1.2  # the single timing source: pulse peak == sound transient


# ---------------------------------------------------------------- pure logic
def slugify(s: str) -> str:
    """'kippo (FU)' -> 'kippo-fu'."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def category_mode(category: str) -> str:
    """Map a category to its styling mode (unknown categories are plain)."""
    cat = category.strip().upper()
    if cat == "QUAD RUNNER":
        return "quad"
    if cat == "PENT RUNNER":
        return "pent"
    return "plain"  # TEAM KILLER explicitly plain per spec; PENT GRIND too


def compute_timing(
    fps: int = FPS,
    duration_s: float = DURATION_S,
    pulse_peak_s: float = PULSE_PEAK_S,
    transient_offset_s: float = 0.0,
) -> dict:
    """Frame/audio timing from the single PULSE_PEAK_S source.

    delay_ms is how far into the card the sound FILE starts so that its
    transient (transient_offset_s into the file) lands on pulse_peak_s.
    """
    delay_s = pulse_peak_s - transient_offset_s
    if delay_s < 0:
        raise ValueError(
            f"transient_offset_s ({transient_offset_s}) exceeds pulse peak ({pulse_peak_s}s)"
        )
    return {
        "total_frames": round(duration_s * fps),
        "pulse_peak_frame": round(pulse_peak_s * fps),
        "delay_ms": round(delay_s * 1000),
    }


def output_basename(category: str, player: str, week: int) -> str:
    return f"week{week:02d}_{slugify(category)}_{slugify(player)}"


def load_sound_for(category: str, config_path: Path = SOUND_CONFIG) -> tuple[Path, float] | None:
    """Resolve (sound path, transient_offset_s) for a category, or None (silent)."""
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    entry = cfg.get("sounds", {}).get(category.strip().upper())
    if not entry:
        return None
    p = Path(entry["path"])
    if not p.is_absolute():
        p = config_path.parent / p
    if not p.is_file():
        raise FileNotFoundError(f"configured sound missing: {p}")
    return p, float(entry.get("transient_offset_s", 0.0))


# ------------------------------------------------------------------- render
def find_chromium() -> str | None:
    """Locate an installed ms-playwright Chromium (the node-side installs work
    fine for python-playwright via executable_path)."""
    import os

    base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    candidates = sorted(base.glob("chromium-*/chrome-win64/chrome.exe"))
    return str(candidates[-1]) if candidates else None


def render_frames(html_path: Path, frames_dir: Path, total_frames: int) -> None:
    from playwright.sync_api import sync_playwright

    frames_dir.mkdir(parents=True, exist_ok=True)
    errs: list[str] = []
    with sync_playwright() as pw:
        exe = find_chromium()
        browser = pw.chromium.launch(headless=True, executable_path=exe)
        page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(html_path.as_uri(), wait_until="load")
        try:
            page.wait_for_function(
                "window.__ready === true || window.__fontError",
                timeout=10000,
            )
        except Exception:
            errs.append("fonts not ready within 10s")
        try:
            font_error = page.evaluate("window.__fontError || null")
            if font_error:
                errs.append(f"font load failed: {font_error}")
        except Exception as exc:
            errs.append(f"failed to evaluate font error: {exc}")
        if errs:
            browser.close()
            raise RuntimeError(f"page errors during render: {errs}")
        for n in range(total_frames):
            page.evaluate("window.step()")
            page.screenshot(path=str(frames_dir / f"f{n:04d}.png"))
        browser.close()
    if errs:
        raise RuntimeError(f"page errors during render: {errs}")


def build_audio_stem(stem_path: Path, sound: tuple[Path, float] | None,
                     duration_s: float, delay_ms: int) -> None:
    """Write the 48 kHz stereo stem: pickup sound delayed to the pulse peak,
    or pure silence for plain categories."""
    if sound is None:
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
               "-t", f"{duration_s}", "-sample_fmt", "s16", str(stem_path)]
    else:
        snd, _off = sound
        af = (f"aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo,"
              f"adelay={delay_ms}:all=1,apad=whole_dur={duration_s}")
        cmd = ["ffmpeg", "-y", "-i", str(snd), "-af", af,
               "-t", f"{duration_s}", str(stem_path)]
    subprocess.run(cmd, check=True, capture_output=True)


def mux_mp4(frames_dir: Path, stem_path: Path, out_mp4: Path,
            fps: int, duration_s: float) -> None:
    cmd = ["ffmpeg", "-y",
           "-framerate", str(fps), "-i", str(frames_dir / "f%04d.png"),
           "-i", str(stem_path),
           "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k",
           "-t", f"{duration_s}", "-movflags", "+faststart", str(out_mp4)]
    subprocess.run(cmd, check=True, capture_output=True)


def make_title_card(category: str, player: str, week: int,
                    out_dir: Path = DEFAULT_OUT_DIR,
                    duration_s: float = DURATION_S, fps: int = FPS,
                    keep_frames: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    mode = category_mode(category)
    sound = load_sound_for(category) if mode in ("quad", "pent") else None
    transient_offset = sound[1] if sound else 0.0
    timing = compute_timing(fps, duration_s, PULSE_PEAK_S, transient_offset)

    fonts = extract_fonts()
    html = build_card_html(
        category=category, player=player, week=week, mode=mode,
        total_frames=timing["total_frames"], fps=fps,
        pulse_peak_frame=timing["pulse_peak_frame"],
        font_face_css=font_face_css(fonts),
    )

    base = output_basename(category, player, week)
    out_mp4 = out_dir / f"{base}.mp4"
    out_wav = out_dir / f"{base}.wav"

    work = Path(tempfile.mkdtemp(prefix="title_card_"))
    try:
        html_path = work / "card.html"
        html_path.write_text(html, encoding="utf-8")
        frames_dir = work / "frames"
        render_frames(html_path, frames_dir, timing["total_frames"])
        build_audio_stem(out_wav, sound, duration_s, timing["delay_ms"])
        mux_mp4(frames_dir, out_wav, out_mp4, fps, duration_s)
        if keep_frames:
            keep = out_dir / f"{base}_frames"
            if keep.exists():
                shutil.rmtree(keep)
            shutil.copytree(frames_dir, keep)
    finally:
        if not keep_frames:
            shutil.rmtree(work, ignore_errors=True)

    return {
        "mp4": str(out_mp4), "wav": str(out_wav), "mode": mode,
        "pulse": mode in ("quad", "pent"),
        "pulse_peak_s": PULSE_PEAK_S if mode in ("quad", "pent") else None,
        "pulse_peak_frame": timing["pulse_peak_frame"] if mode in ("quad", "pent") else None,
        "sound": str(sound[0]) if sound else None,
        **{k: timing[k] for k in ("total_frames", "delay_ms")},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate an OF THE WEEK title card (mp4 + wav stem).")
    ap.add_argument("--category", required=True, help='e.g. "QUAD RUNNER"')
    ap.add_argument("--player", required=True, help='e.g. "kippo (FU)"')
    ap.add_argument("--week", required=True, type=int)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--duration", type=float, default=DURATION_S, help="card length in seconds")
    ap.add_argument("--fps", type=int, default=FPS)
    ap.add_argument("--keep-frames", action="store_true", help="copy the PNG frames next to the mp4")
    args = ap.parse_args()

    info = make_title_card(args.category, args.player, args.week,
                           out_dir=args.out_dir, duration_s=args.duration,
                           fps=args.fps, keep_frames=args.keep_frames)
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
