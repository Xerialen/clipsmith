#!/usr/bin/env python3
r"""clipsmith Phase-3 driver (issue #26): selected candidate -> branded MP4.

ONE command, zero manual steps in between:

    python scripts/produce_clip.py <candidate-id>

runs, in sequence, the existing stage CLIs (subprocess-chained, never
reimplemented):

    1. render      python -m clipsmith.render                (#19, ~18 min)
    2. title_card  scripts/assembly/title_card/make_title_card.py   (#23)
    3. killcounter scripts/assembly/killcounter/generate.py         (#24)
    4. assemble    scripts/assembly/resolve_assembly/assemble.py    (#25)

Entry condition (the honest Phase-2 seam): the candidate is `selected` in the
store. The render stage flips it to `rendered`; the finished file lands in
C:\Users\example\Videos\clipsmith\branded\<stem>_assembled.mp4.

Skip-if-already-rendered: when the raw capture already exists in the raw dir
(any raw_capture_names variant), the ~18 min render stage is skipped. If the
candidate is still `selected` at that point the driver performs the
selected -> rendered status flip itself (the flip the orchestrator would have
done); a candidate already `rendered` with its raw present simply resumes the
remaining stages (crash-resume case).

FPS coherence (issue-22 lock: timeline fps = clip fps, never retime): the
title-card and kill-counter generators default to 30 fps, but the locked
capture is 60 — the driver probes the raw capture with ffprobe and drives
BOTH generators at the raw fps, so every generated asset matches the timeline
exactly. The intro is conformed too: Resolve does NOT cleanly 2:1-conform a
30 fps clip under assemble.py's endFrame convention (measured 2026-06-11 on
the BLooD_DoG run: intro/outro segments came out 420 timeline frames instead
of 210 — 7 s each, 52.5 s total vs the planned 45.5). When the configured
intro's fps differs from the capture fps, the driver pre-conforms it with
ffmpeg (exact frame doubling, audio copied) into the assets dir and hands it
to assemble.py via its --intro drop-in; the committed placeholder
(assets/placeholder/intro.mp4, HITL #13's drop-in point) is never modified.

Fail-fast: any non-zero stage exit aborts with the stage name (assemble exit 3
is the known Resolve cold-launch modal — click it once, re-run). Per-stage
wall-clock is printed at the end.

Render isolation: this driver renders on its own Xvfb display :97 with its
own ext4 staging (/root/render-97) inside the clipsmith-render distro —
parallel to clipsmith.render's default :99/render-A, so an ad-hoc render
can't collide with a production run.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

SCRIPTS_DIR = REPO_ROOT / "scripts"
RENDER_MODULE = "clipsmith.render"
TITLE_CLI = SCRIPTS_DIR / "assembly" / "title_card" / "make_title_card.py"
KILLCOUNTER_CLI = SCRIPTS_DIR / "assembly" / "killcounter" / "generate.py"
ASSEMBLE_CLI = SCRIPTS_DIR / "assembly" / "resolve_assembly" / "assemble.py"

OUTPUT_ROOT = Path(
    os.environ.get("CLIPSMITH_OUTPUT_ROOT", Path.home() / "Videos" / "clipsmith")
)
DEFAULT_RAW_DIR = Path(os.environ.get("CLIPSMITH_RAW_DIR", OUTPUT_ROOT / "raw"))
DEFAULT_ASSETS_DIR = Path(
    os.environ.get("CLIPSMITH_ASSETS_DIR", OUTPUT_ROOT / "assets" / "generated")
)
DEFAULT_OUT_DIR = Path(os.environ.get("CLIPSMITH_FINAL_DIR", OUTPUT_ROOT / "branded"))

# assemble.py's intro drop-in (HITL #13). Mirrored here so the driver can
# fps-conform it BEFORE assembly; never modified in place.
DEFAULT_INTRO = REPO_ROOT / "assets" / "placeholder" / "intro.mp4"

# This driver's render isolation (see module docstring).
DEFAULT_DISPLAY = 97
DEFAULT_STAGING_ROOT = "/root/render-97"

STAGE_ORDER = ("render", "title_card", "killcounter", "intro_conform",
               "assemble")
EXIT_RESOLVE_COLD_LAUNCH = 3  # assemble.py EXIT_RESOLVE


def _load_ra_logic():
    """The Resolve-assembly naming/week logic, loaded by path (both assembly
    stage dirs ship a `logic.py`; an importlib spec avoids the sys.path
    module-name collision)."""
    spec = importlib.util.spec_from_file_location(
        "ra_logic", ASSEMBLE_CLI.parent / "logic.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ra_logic = _load_ra_logic()


# ----------------------------------------------------------------- pure logic

def parse_fps(r_frame_rate):
    """ffprobe r_frame_rate -> integer generator fps: '60/1' -> 60.

    The generators take integer --fps; anything non-integral (NTSC rates
    can't come out of the locked capture chain) is rejected loudly rather
    than silently mis-timed.
    """
    num, den = str(r_frame_rate).split("/")
    fps = float(num) / float(den)
    if abs(fps - round(fps)) > 1e-6:
        raise ValueError(f"non-integer capture fps {fps} ({r_frame_rate}) — "
                         "generators need integer fps; check the raw capture")
    return int(round(fps))


def derive_week(matchdate):
    """ISO week of the match date (same derivation the assembly's artifact
    lookup uses — title_basename must agree with the title-card CLI)."""
    return ra_logic.week_number(matchdate)


def find_raw(cand, raw_dir):
    """First existing raw-capture variant for the candidate, or None."""
    raw_dir = Path(raw_dir)
    for name in ra_logic.raw_capture_names(cand):
        p = raw_dir / name
        if p.exists():
            return p
    return None


def render_decision(status, raw_exists):
    """What to do about the render stage.

    -> 'run'        status selected, no raw yet: run the orchestrator
    -> 'skip-flip'  raw already present, still selected: skip the render,
                    flip selected -> rendered ourselves
    -> 'skip'       raw present and already rendered: resume the rest
    Raises ValueError for anything else (wrong entry condition).
    """
    if status == "selected":
        return "skip-flip" if raw_exists else "run"
    if status == "rendered" and raw_exists:
        return "skip"
    if status == "rendered":
        raise ValueError(
            "candidate is 'rendered' but its raw capture is missing from the "
            "raw dir — restore the raw or re-walk the status")
    raise ValueError(
        f"candidate status is {status!r}, need 'selected' (fake the Discord "
        f"selection with: python -m clipsmith.store update-status <id> posted; "
        f"then ... selected)")


def intro_conform_decision(intro_exists, intro_fps, target_fps):
    """What to do about the intro before assembly.

    -> 'default'   no intro file: let assemble.py derive its placeholder
                   (from the — already fps-coherent — title card)
    -> 'use'       intro fps matches the capture: pass through untouched
    -> 'conform'   fps mismatch: pre-conform a copy at the capture fps
                   (Resolve does NOT cleanly 2:1-conform under assemble.py's
                   endFrame convention — see module docstring)
    """
    if not intro_exists:
        return "default"
    return "use" if intro_fps == target_fps else "conform"


def conform_intro_command(src, dst, fps):
    """Exact-duplication fps conform (30 -> 60 doubles every frame); the
    audio stream is copied bit-for-bit. Always re-encoded from src so a
    HITL #13 intro swap can never be shadowed by a stale cache."""
    return ["ffmpeg", "-y", "-v", "error", "-i", str(src),
            "-vf", f"fps={fps}", "-c:v", "libx264", "-crf", "18",
            "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", str(dst)]


def render_command(cand_id, db=None, display=DEFAULT_DISPLAY,
                   staging_root=DEFAULT_STAGING_ROOT, raw_dir=None,
                   python=None):
    argv = [python or sys.executable, "-m", RENDER_MODULE, cand_id,
            "--display", str(display), "--staging-root", str(staging_root)]
    if raw_dir is not None:
        argv += ["--final-dir", str(raw_dir)]
    if db:
        argv += ["--db", str(db)]
    return argv


def stage_commands(cand, fps, raw_dir, assets_dir, out_dir, db=None,
                   python=None, intro=None):
    """argv lists for the post-render stages, fps-coherent with the capture.

    `intro`, when given, is the (already fps-coherent) intro passed through
    to assemble.py's --intro drop-in.
    """
    py = python or sys.executable
    week = derive_week(cand["matchdate"])
    title = [py, str(TITLE_CLI),
             "--category", cand["category"],
             "--player", cand["pov_player"]["name"],
             "--week", str(week),
             "--fps", str(fps),
             "--out-dir", str(assets_dir)]
    killcounter = [py, str(KILLCOUNTER_CLI), cand["id"],
                   "--fps", str(fps),
                   "--outdir", str(assets_dir)]
    assemble = [py, str(ASSEMBLE_CLI), cand["id"],
                "--raw-dir", str(raw_dir),
                "--assets-dir", str(assets_dir),
                "--out-dir", str(out_dir)]
    if intro is not None:
        assemble += ["--intro", str(intro)]
    if db:
        killcounter += ["--db", str(db)]
        assemble += ["--db", str(db)]
    return {"title_card": title, "killcounter": killcounter,
            "assemble": assemble}


class StageError(Exception):
    """A stage exited non-zero. Carries the stage name + timings so far."""

    def __init__(self, stage, code, timings):
        super().__init__(f"stage '{stage}' failed with exit code {code}")
        self.stage = stage
        self.code = code
        self.timings = timings


def run_pipeline(stages, runner, clock=time.monotonic):
    """Run [(name, argv)] in order, fail-fast, timing each stage.

    `runner(name, argv) -> exit code` is injected (tests use a fake; main
    uses a live-streaming subprocess). Returns {name: wall_seconds}.
    """
    timings = {}
    for name, argv in stages:
        t0 = clock()
        code = runner(name, argv)
        timings[name] = clock() - t0
        if code != 0:
            raise StageError(name, code, timings)
    return timings


def format_timings(timings, order=STAGE_ORDER):
    """Per-stage wall-clock table (mm:ss), skipped stages marked."""
    lines = ["per-stage wall-clock:"]
    total = 0.0
    for name in order:
        if name not in timings:
            continue
        secs = timings[name]
        total += secs
        mark = f"{int(secs // 60):3d}:{secs % 60:04.1f}" if secs else "  skipped"
        lines.append(f"  {name:<12} {mark}")
    lines.append(f"  {'TOTAL':<12} {int(total // 60):3d}:{total % 60:04.1f}")
    return "\n".join(lines)


# ------------------------------------------------------------------ live run

def live_runner(name, argv):
    print(f"\n=== stage: {name} ===\n+ {' '.join(str(a) for a in argv)}",
          flush=True)
    return subprocess.run([str(a) for a in argv]).returncode


def probe_fps(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    return parse_fps(json.loads(out)["streams"][0]["r_frame_rate"])


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="produce_clip.py",
        description="Phase-3 driver: selected candidate -> branded MP4, "
                    "hands-off (render -> title card -> kill counter -> "
                    "Resolve assembly)")
    ap.add_argument("candidate_id")
    ap.add_argument("--db", help="store DB path (default: state/candidates.db)")
    ap.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    ap.add_argument("--assets-dir", default=str(DEFAULT_ASSETS_DIR))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--display", type=int, default=DEFAULT_DISPLAY,
                    help="Xvfb display for the render stage")
    ap.add_argument("--staging-root", default=DEFAULT_STAGING_ROOT,
                    help="ext4 staging dir for the render stage")
    args = ap.parse_args(argv)

    from clipsmith import store

    cand = store.get_candidate(args.candidate_id, db_path=args.db)
    print(f"produce_clip: {cand['id']}: {cand['category']} — "
          f"{cand['pov_player']['name']} on {cand['map']} "
          f"({cand['matchdate'][:10]}, score {cand['score']}) "
          f"[status: {cand['status']}]", flush=True)

    raw = find_raw(cand, args.raw_dir)
    try:
        decision = render_decision(cand["status"], raw is not None)
    except ValueError as e:
        sys.exit(f"error: {e}")

    timings = {}
    if decision == "run":
        try:
            timings.update(run_pipeline(
                [("render", render_command(cand["id"], db=args.db,
                                           display=args.display,
                                           staging_root=args.staging_root,
                                           raw_dir=args.raw_dir))],
                live_runner))
        except StageError as e:
            sys.exit(f"error: {e} — aborting before later stages")
        raw = find_raw(cand, args.raw_dir)
        if raw is None:
            sys.exit("error: render stage succeeded but no raw capture found "
                     f"under {args.raw_dir} — naming drift?")
    else:
        print(f"render: SKIPPED — raw capture already present: {raw.name}")
        timings["render"] = 0.0
        if decision == "skip-flip":
            store.update_status(cand["id"], "rendered", db_path=args.db)
            print(f"store: {cand['id']} -> rendered "
                  "(render skipped, raw already on Drive)")

    fps = probe_fps(raw)
    print(f"fps: raw capture is {fps} fps -> driving title card + kill "
          f"counter at {fps} (timeline fps follows the clip, never retimed)",
          flush=True)

    intro = None
    intro_decision = intro_conform_decision(
        DEFAULT_INTRO.exists(),
        probe_fps(DEFAULT_INTRO) if DEFAULT_INTRO.exists() else None, fps)
    stages = []
    if intro_decision == "conform":
        intro = Path(args.assets_dir) / f"intro_{fps}fps.mp4"
        print(f"intro: {DEFAULT_INTRO.name} fps != capture fps -> "
              f"conforming to {intro.name} (assemble.py --intro drop-in)")
        stages.append(("intro_conform",
                       conform_intro_command(DEFAULT_INTRO, intro, fps)))
    elif intro_decision == "default":
        print("intro: no placeholder on disk — assemble.py derives one "
              "from the title card")

    cmds = stage_commands(cand, fps, args.raw_dir, args.assets_dir,
                          args.out_dir, db=args.db, intro=intro)
    stages += [(n, cmds[n]) for n in ("title_card", "killcounter", "assemble")]
    try:
        timings.update(run_pipeline(stages, live_runner))
    except StageError as e:
        timings.update(e.timings)
        hint = ""
        if e.stage == "assemble" and e.code == EXIT_RESOLVE_COLD_LAUNCH:
            hint = (" — Resolve cold-launch modal (the one known manual "
                    "step): click it in the Resolve window, then re-run "
                    "this same command (completed stages resume/skip)")
        print("\n" + format_timings(timings), flush=True)
        sys.exit(f"error: {e}{hint}")

    output = ra_logic.resolve_artifacts(
        cand, Path(args.raw_dir), Path(args.assets_dir),
        Path(args.out_dir))["output"]
    if not output.exists():
        sys.exit(f"error: pipeline finished but {output} is missing")

    final_status = store.get_candidate(cand["id"], db_path=args.db)["status"]
    print("\n" + format_timings(timings))
    print(json.dumps({
        "candidate": cand["id"],
        "status": final_status,
        "fps": fps,
        "raw": str(raw),
        "output": str(output),
        "bytes": output.stat().st_size,
        "stage_seconds": {k: round(v, 1) for k, v in timings.items()},
    }, indent=2))


if __name__ == "__main__":
    main()
