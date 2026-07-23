#!/usr/bin/env python3
r"""clipsmith kill-counter overlay generator (issue #24, P3.5).

Candidate id (or raw candidate JSON) -> a self-contained overlay pair the
Resolve assembly (P3.6) stacks on the clip track with no per-kill logic:

    <basename>_killcounter.mov       1920x1080@30 QuickTime Animation (qtrle,
                                     pix_fmt argb) — lossless RLE WITH alpha,
                                     Resolve-ingestible, tiny for graphics
    <basename>_killcounter_sfx.wav   48 kHz stereo audio stem, same duration,
                                     per-kill pop / per-teamkill "oops" at the
                                     frame-quantized visual tick times

Usage:
    python scripts/assembly/killcounter/generate.py <candidate-id>
    python scripts/assembly/killcounter/generate.py --json cand.json
    ... [--mode window|capture] [--preroll 5] [--countdown 10] [--fps 30]
        [--outdir DIR] [--workdir DIR] [--keep-workdir]

Alignment (see logic.overlay_window): default --mode window puts t0 at
window.start_s (kill i appears at t_s - start_s, duration == window length);
--mode capture mirrors render_candidate.plan_render so the overlay covers
the raw capture incl. preroll 1:1.

All timing/counter logic is pure (logic.py, unit-tested); this driver only
shells out: node (build_overlay.mjs + Playwright render_frames.mjs) and
ffmpeg (qtrle encode, SFX synthesis, stem mix). SFX are SYNTHESIZED
(layered-sine plus noise pattern, no licensed assets) into
sfx/PLACEHOLDER_*.wav next to this script; swap real sounds in via
logic.SFX_FILES — the drop-in config point.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))

import logic  # noqa: E402

OUTPUT_ROOT = Path(
    os.environ.get("CLIPSMITH_OUTPUT_ROOT", Path.home() / "Videos" / "clipsmith")
)
DEFAULT_OUTDIR = os.environ.get(
    "CLIPSMITH_ASSETS_DIR", str(OUTPUT_ROOT / "assets" / "generated")
)
# Brand fonts come out of the OneMoreQUAD bundle via the proven (read-only)
# scripts/branding/pick_fonts2.mjs extractor — Nightrunner gunzip gotcha incl.
BRANDING_DIR = REPO_ROOT / "scripts" / "branding"
_brand_scenes_dir = os.environ.get("CLIPSMITH_BRAND_SCENES_DIR")
BRAND_SCENE = (
    Path(_brand_scenes_dir) / "QUAD RUNNER - Starting Soon (1920x1080).html"
    if _brand_scenes_dir else None
)
FONT_FILES = (
    "qrf_Nightrunner-700-normal.woff2",
    "qrf_ChakraPetch-700-italic.woff2",
    "qrf_ChakraPetch-700-normal.woff2",
    "qrf_ShareTechMono-400-normal.woff2",
)


def run(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def ensure_node_deps():
    """Playwright npm package local to this dir (node_modules gitignored)."""
    if (HERE / "node_modules" / "playwright").exists():
        return
    run(["npm", "install", "--no-audit", "--no-fund"], cwd=HERE, shell=(sys.platform == "win32"))


def ensure_fonts(workdir):
    if all((workdir / f).exists() for f in FONT_FILES):
        return
    if BRAND_SCENE is None:
        sys.exit("error: set CLIPSMITH_BRAND_SCENES_DIR to the OBS scenes directory")
    if not BRAND_SCENE.exists():
        sys.exit(f"error: brand scene not found: {BRAND_SCENE}")
    run(["node", BRANDING_DIR / "pick_fonts2.mjs", BRAND_SCENE, workdir])


def ensure_sfx():
    """Synthesize the placeholder SFX once (small wavs, committed).

    Kill pop: layered sines (920+1380 Hz) + high-passed noise click, fast
    exponential decay — punchy tick. Oops: descending two-tone (392->277 Hz)
    with vibrato over a low brown-noise thud — comic "wah". Both follow the
    FFmpeg synthesis pattern (no external sourcing or licensing).
    """
    sfx_dir = HERE / "sfx"
    sfx_dir.mkdir(exist_ok=True)
    pop = sfx_dir / logic.SFX_FILES["kill"]
    oops = sfx_dir / logic.SFX_FILES["oops"]
    if not pop.exists():
        run(["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "sine=frequency=920:duration=0.22",
             "-f", "lavfi", "-i", "sine=frequency=1380:duration=0.22",
             "-f", "lavfi", "-i", "anoisesrc=d=0.06:color=white:amplitude=0.6:seed=7",
             "-filter_complex",
             "[0]volume=0.9[a];[1]volume=0.5[b];"
             "[2]highpass=f=2500,volume=0.8[c];"
             "[a][b][c]amix=inputs=3:normalize=0,"
             "afade=t=out:st=0.02:d=0.20:curve=exp,volume=2.0,"
             "alimiter=limit=0.95",
             "-ar", "48000", "-ac", "2", pop])
    if not oops.exists():
        run(["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "sine=frequency=392:duration=0.28",
             "-f", "lavfi", "-i", "sine=frequency=277:duration=0.40",
             "-f", "lavfi", "-i", "anoisesrc=d=0.5:color=brown:amplitude=0.35:seed=9",
             "-filter_complex",
             "[0]vibrato=f=9:d=0.4,volume=0.9[a];"
             "[1]adelay=220|220,vibrato=f=7:d=0.5,volume=0.95[b];"
             "[2]lowpass=f=500,volume=0.5[c];"
             "[a][b][c]amix=inputs=3:normalize=0,"
             "afade=t=out:st=0.40:d=0.25,volume=1.8,"
             "alimiter=limit=0.95",
             "-ar", "48000", "-ac", "2", oops])
    return sfx_dir


def encode_overlay(workdir, fps, out_mov):
    run(["ffmpeg", "-y", "-v", "error",
         "-framerate", fps, "-i", workdir / "frames" / "f%05d.png",
         "-c:v", "qtrle", "-pix_fmt", "argb", out_mov])


def mix_stem(sfx_dir, schedule, duration_s, out_wav):
    """anullsrc base of exact duration + each SFX adelay'd to its tick."""
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-t", f"{duration_s:.3f}",
           "-i", "anullsrc=r=48000:cl=stereo"]
    chains = []
    for i, ev in enumerate(schedule):
        cmd += ["-i", sfx_dir / logic.SFX_FILES[ev["sound"]]]
        ms = int(round(ev["offset_s"] * 1000))
        pitch = ev["pitch"]
        steps = [f"adelay={ms}|{ms}"] if ms else ["acopy"]
        if abs(pitch - 1.0) > 1e-6:
            # rising combo pitch: resample trick (shorter+higher — wanted)
            steps = [f"asetrate=48000*{pitch}", "aresample=48000"] + steps
        chains.append(f"[{i + 1}]{','.join(steps)}[s{i}]")
    n = len(schedule)
    mix = "[0]" + "".join(f"[s{i}]" for i in range(n))
    chains.append(f"{mix}amix=inputs={n + 1}:normalize=0:dropout_transition=0,"
                  f"alimiter=limit=0.95[out]")
    cmd += ["-filter_complex", ";".join(chains),
            "-map", "[out]", "-t", f"{duration_s:.3f}",
            "-ar", "48000", "-ac", "2", out_wav]
    run(cmd)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="generate.py",
        description="kill-counter overlay generator: candidate -> alpha mov + sfx wav")
    ap.add_argument("candidate_id", nargs="?", help="candidate id in the store")
    ap.add_argument("--json", help="raw candidate JSON file (testing; bypasses store)")
    ap.add_argument("--db", help="store DB path (default: state/candidates.db)")
    ap.add_argument("--mode", choices=("window", "capture"), default="window")
    ap.add_argument("--preroll", type=float, default=logic.DEFAULT_PREROLL_S)
    ap.add_argument("--countdown", type=float, default=logic.DEFAULT_COUNTDOWN_S)
    ap.add_argument("--fps", type=int, default=logic.DEFAULT_FPS)
    ap.add_argument("--outdir", default=DEFAULT_OUTDIR)
    ap.add_argument("--workdir", help="frame/work dir (default: temp, removed)")
    ap.add_argument("--keep-workdir", action="store_true")
    args = ap.parse_args(argv)

    if bool(args.candidate_id) == bool(args.json):
        ap.error("give exactly one of <candidate-id> or --json FILE")
    if args.json:
        cand = json.loads(Path(args.json).read_text(encoding="utf-8"))
    else:
        from clipsmith import store
        cand = store.get_candidate(args.candidate_id, db_path=args.db)

    events = logic.extract_kill_events(cand)
    t0, duration_s = logic.overlay_window(
        cand, mode=args.mode, preroll=args.preroll, countdown=args.countdown)
    timeline = logic.build_timeline(events, t0, duration_s, fps=args.fps)
    sched = logic.sfx_schedule(timeline)
    base = logic.output_basename(cand)
    print(f"candidate {cand.get('id', '?')}: {cand['category']} — "
          f"{cand['pov_player']['name']} on {cand['map']}; "
          f"mode={args.mode} t0={t0:.3f} duration={duration_s:.3f}s "
          f"({timeline['total_frames']} frames @{args.fps})")
    for ev in timeline["events"]:
        print(f"  f{ev['frame']:05d} {ev['offset_s']:7.3f}s {ev['type']:4s} "
              f"count={ev['count']} victim={ev['victim']}")
    for d in timeline["dropped"]:
        print(f"  DROPPED t_s={d['t_s']} ({d['reason']})")

    workdir = Path(args.workdir) if args.workdir else \
        Path(tempfile.mkdtemp(prefix="killcounter-"))
    workdir.mkdir(parents=True, exist_ok=True)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    try:
        ensure_node_deps()
        ensure_fonts(workdir)
        sfx_dir = ensure_sfx()
        (workdir / "schedule.json").write_text(json.dumps(timeline), encoding="utf-8")
        (workdir / "dynamic.json").write_text(
            json.dumps(logic.dynamic_frames(timeline)), encoding="utf-8")
        run(["node", HERE / "build_overlay.mjs", workdir])
        run(["node", HERE / "render_frames.mjs", workdir])
        out_mov = outdir / f"{base}.mov"
        out_wav = outdir / f"{base}_sfx.wav"
        encode_overlay(workdir, args.fps, out_mov)
        if sched:
            mix_stem(sfx_dir, sched, duration_s, out_wav)
        else:  # zero-event window: silent stem keeps the pair contract
            run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-t", f"{duration_s:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
                 "-ar", "48000", "-ac", "2", out_wav])
        print(json.dumps({
            "candidate": cand.get("id"), "mode": args.mode,
            "t0": t0, "duration_s": duration_s,
            "events": len(timeline["events"]), "dropped": len(timeline["dropped"]),
            "overlay": str(out_mov), "sfx_stem": str(out_wav),
        }))
    finally:
        if not args.keep_workdir and not args.workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        elif args.keep_workdir or args.workdir:
            print(f"workdir kept: {workdir}")


if __name__ == "__main__":
    main()
