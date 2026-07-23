#!/usr/bin/env python3
r"""clipsmith render orchestrator: candidate id -> raw capture on Drive (#19).

One hands-off command turns a candidate from the store (state/candidates.db)
into a raw 1920x1080@60 mov under G:\\My Drive\\captures\\quake\\highlights\\raw\\
(the issue-22 locked picture settings — see docs/picture-settings.md):

    python -m clipsmith.render <candidate-id>
    # compatibility wrapper: python scripts/render/render_candidate.py <candidate-id>

Pipeline (all render gotchas from docs/testing-and-validation.md 2026-06-09/10 are
baked in here and in scripts/render/render_worker.sh — see the worker header):

    store.get_candidate  ->  demo (state/demos/<sha>.mvd.gz, CDN fallback via
    clipsmith.watcher)   ->  generated cfg pair (re_trigger "matchdate:" arm
    cfg + exec'd seek/track/capture cfg) + the parity HUD layer (#20)  ->
    headless ezQuake under Xvfb in the clipsmith-render WSL distro (classic
    renderer, ext4-staged basedir, early frame validation)  ->  ffmpeg
    PNG+wav assembly (H.264 CRF 14 + PCM, the Resolve ingest master)  ->
    staged to CLIPSMITH_STAGE_DIR  ->  moved to CLIPSMITH_RAW_DIR  ->
    candidate status selected -> rendered.

Timeline math (the recurring units gotcha): the candidate window is
MATCH-relative seconds; the demo timeline starts ~10 s earlier (KTX countdown
— same DEFAULT_COUNTDOWN_S as hub_links.py, owner-verified 2026-06-10), and
`demo_jump N` is absolute from demo start (newdemotime = demostarttime + N,
ezquake cl_demo.c:5260). So:

    seek   = floor(window.start_s + countdown - preroll)      [demo seconds]
    length = ceil(window.end_s + countdown - seek)            [capture secs]

Status: production triggers on `selected` candidates and flips them to
`rendered` on success. If the exact raw output already exists, reruns skip the
worker and either flip `selected -> rendered` or no-op an already `rendered`
candidate. --dry-run prints that decision without writing files or status.
--force-status renders a candidate in any status (testing); the final flip is
then attempted but skipped if illegal.

Renders run ONLY in the clipsmith-render distro — never on Ubuntu-24.04,
never on the Windows GPU/display stack (user veto). Outputs never land in
the repo or C:\\nQuake (C:\\nQuake is used read-only as a loose-map source).
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_RENDER_DIR = REPO_ROOT / "scripts" / "render"

DEFAULT_COUNTDOWN_S = 10.0   # KTX standard, matches clipsmith/hub_links.py
DEFAULT_PREROLL_S = 5.0
# Locked picture settings (issue #22, docs/picture-settings.md): capture
# 1920x1080@60, HUD drawn in a 720p console space (parity_hud.cfg element
# scales were tuned at 720p; vid_conwidth/vid_conheight lock keeps the layout
# identical at any output resolution).
DEFAULT_FPS = 60
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
HUD_CON_WIDTH = 1280
HUD_CON_HEIGHT = 720
HUD_CFG_NAME = "parity_hud.cfg"    # the #20 hub-parity layer, production HUD
DEFAULT_DISPLAY = 99         # this orchestrator's Xvfb display (parallel jobs use others)
DEFAULT_DISTRO = "clipsmith-render"
DEFAULT_STAGING_ROOT = "/root/render-A"
DEFAULT_ENGINE_DIR = "/root/ezq"
DEFAULT_BASE_CONTENT = "/root/nquake"
DEFAULT_OUTPUT_ROOT = Path(
    os.environ.get("CLIPSMITH_OUTPUT_ROOT", Path.home() / "Videos" / "clipsmith")
)
DEFAULT_STAGE_DIR = os.environ.get(
    "CLIPSMITH_STAGE_DIR", str(DEFAULT_OUTPUT_ROOT / "stage")
)
DEFAULT_FINAL_DIR = os.environ.get(
    "CLIPSMITH_RAW_DIR", str(DEFAULT_OUTPUT_ROOT / "raw")
)
DEFAULT_MAP_SOURCE_DIR = os.environ.get(
    "CLIPSMITH_MAP_SOURCE_DIR", str(Path.home() / "Quake" / "qw" / "maps")
)  # read-only loose-map source
DEFAULT_HARD_TIMEOUT = 1800  # 1080p60 capture of a 40s window runs ~8-10 min
DEFAULT_EARLY_FRAME = 200

DEMO_NAME = "clipsmith_job.mvd"

# Durable failure record: a failed render leaves the
# candidate in 'selected' (correct — it IS still selected), but the failure
# must be visible to schedulers so they don't blind-retry forever. Each
# failure appends one JSONL line; P5.3 idempotency hardening formalizes
# retry budgets on top of this.
FAILURES_LOG = REPO_ROOT / "state" / "render-failures.jsonl"


def record_render_failure(cand_id, code, message, db_path=None):
    from datetime import datetime, timezone
    FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "candidate": cand_id,
        "exit_code": code,
        "error": message,
        "db": str(db_path) if db_path else None,
    }
    with open(FAILURES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"failure recorded: {FAILURES_LOG}", file=sys.stderr)


# ----------------------------------------------------------- pure logic

def slugify(text):
    """Lowercase, non-alphanumeric -> '-', collapsed: 'rgh   (FU)' -> 'rgh-fu'."""
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def output_basename(cand):
    """Raw-capture naming with candidate id for overwrite safety.

    e.g. 2026-06-09_e1m2_red-vs-blue_rusti-fu-quad-runner_cand0001_1pov_raw

    The pre-#53 filename omitted the candidate id. That silently overwrote
    same-match/same-player/same-category renders; Resolve lookup keeps the
    old name as a fallback, but new raw captures include cid8.
    """
    matchdate = re.sub(r"[^0-9-]", "", str(cand["matchdate"])[:10])
    teams = "-vs-".join(slugify(t["name"]) for t in cand["teams"])
    situation = slugify(f"{cand['pov_player']['name']} {cand['category']}")
    cid = slugify(str(cand["id"])[:8])
    return f"{matchdate}_{slugify(cand['map'])}_{teams}_{situation}_{cid}_1pov_raw"


def final_output_path(cand, final_dir):
    """Canonical raw capture path for this render invocation."""
    return Path(final_dir) / f"{output_basename(cand)}.mov"


def usable_raw_exists(path):
    """Return True only for a reusable completed raw capture artifact."""
    path = Path(path)
    return path.is_file() and path.stat().st_size > 0


def render_decision(status, final_exists, force_status=False):
    """Return how the render stage should proceed for a candidate.

    -> 'run'        selected and no canonical raw file exists: run worker
    -> 'skip-flip'  selected but raw exists: skip worker, flip to rendered
    -> 'skip'       rendered and raw exists: rerun no-op
    """
    if force_status:
        return "run"
    if status == "selected":
        return "skip-flip" if final_exists else "run"
    if status == "rendered" and final_exists:
        return "skip"
    if status == "rendered":
        raise ValueError(
            "candidate is 'rendered' but its canonical raw capture is missing "
            "from the final dir -- restore the raw or re-walk the status"
        )
    raise ValueError(
        f"candidate status is {status!r}, need 'selected' "
        "(production renders trigger on selection; --force-status to override)"
    )


def plan_render(cand, preroll=DEFAULT_PREROLL_S, countdown=DEFAULT_COUNTDOWN_S,
                fps=DEFAULT_FPS):
    """Window (match-relative) -> demo_jump seek + capture duration (+frames).

    seek_s is clamped at 0 for windows that start at the match start (the
    countdown still gives them up to `countdown` seconds of preroll).
    """
    start = float(cand["window"]["start_s"])
    end = float(cand["window"]["end_s"])
    if end <= start:
        raise ValueError(f"degenerate window: start_s={start} end_s={end}")
    seek_s = max(0, math.floor(start + countdown - preroll))
    capture_s = math.ceil(end + countdown - seek_s)
    return {
        "seek_s": seek_s,
        "capture_s": capture_s,
        "expected_frames": capture_s * fps,
        "userid": int(cand["pov_player"]["userid"]),
    }


def hud_space_lines(width, height):
    """Console-space lock for the parity HUD layer (issue #22).

    parity_hud.cfg's element scales were tuned at 720p. ezQuake draws all 2D
    in the vid_conwidth/vid_conheight coordinate space, so locking that space
    to 1280x720 reproduces the tuned layout pixel-proportionally at any
    capture resolution. At <=720p the console space equals the framebuffer
    already — emit nothing.
    """
    if height <= HUD_CON_HEIGHT and width <= HUD_CON_WIDTH:
        return []
    return [f"vid_conwidth {HUD_CON_WIDTH}", f"vid_conheight {HUD_CON_HEIGHT}"]


def build_arm_cfg(demo_name, fps=DEFAULT_FPS, width=DEFAULT_WIDTH,
                  height=DEFAULT_HEIGHT):
    """First cfg of the pair: exec'd from the command line; arms the trigger.

    demo_jump/track/demo_capture need cls.state == ca_active (cl_demo.c:4859);
    the KTX "matchdate:" console line prints exactly once, when fully active,
    and re_trigger runs the same-named alias on the match (tp_triggers.c:376).
    The alias itself only exec's the second cfg — exec inserts into cbuf_main,
    escaping the cbuf_safe trigger-context blacklist (cmd.c:1762).

    The parity HUD layer (#20) is exec'd here, after the base view settings
    (it overrides fov/crosshair on purpose — hub values win), followed by the
    720p console-space lock when capturing above 720p.
    """
    return "\n".join([
        "cfg_save_onquit 0",
        f"demo_capture_fps {fps}",
        "demo_capture_dir capture",
        "fov 100",
        "crosshair 1",
        "mvd_autotrack 0",
        "demo_autotrack 0",
        "cl_hightrack 0",
        "con_notify 0",
        f"exec {HUD_CFG_NAME}",
        *hud_space_lines(width, height),
        'alias w5 "wait; wait; wait; wait; wait"',
        'alias w25 "w5; w5; w5; w5; w5"',
        'alias clipsmith_arm "re_trigger_disable clipsmith_arm; '
        'echo CLIPSMITH_ARMED; exec clipsmith_capgo.cfg"',
        're_trigger clipsmith_arm "matchdate:"',
        'alias f_demoend "echo CLIPSMITH_DEMOEND"',
        "echo clipsmith_armed",
        f"playdemo demos/{demo_name}",
        "",
    ])


def build_capgo_cfg(seek_s, userid, capture_s):
    """Second cfg of the pair: seek, lock the POV, start the capture.

    Order is load-bearing: autotrack must be zeroed BEFORE the jump (MVD
    autotrack re-locks its own pick), and `track <uid>` must come AFTER the
    seek lands (Cam_Lock clears its own lock flag, cl_cam.c:202).
    """
    return "\n".join([
        "echo CLIPSMITH_SEEK",
        "mvd_autotrack 0",
        "demo_autotrack 0",
        "cl_hightrack 0",
        f"demo_jump {seek_s}",
        "w25",
        "w25",
        f"track {userid}",
        "w5",
        "toggleconsole",
        "con_notify 0",
        f"demo_capture start {capture_s}",
        "echo CLIPSMITH_CAPSTART",
        "",
    ])


def win_to_wsl(path):
    r"""C:\foo\bar -> /mnt/c/foo/bar."""
    p = str(path).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if not m:
        raise ValueError(f"not an absolute Windows path: {path}")
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}"


def build_job_env(params):
    """KEY='value' lines for render_worker.sh to source."""
    lines = []
    for key, value in params.items():
        value = str(value)
        if "'" in value or "\n" in value:
            raise ValueError(f"unsafe job-env value for {key}: {value!r}")
        lines.append(f"{key}='{value}'")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ host steps

def ensure_demo(cand, demos_dir):
    """Prefer the watcher-downloaded demo; fall back to the d.quake.world CDN."""
    from clipsmith import watcher
    demos_dir = Path(demos_dir)
    local = demos_dir / f"{cand['demo_sha']}.mvd.gz"
    if local.exists() and local.stat().st_size > 0:
        print(f"demo: using existing {local}")
        return local
    print(f"demo: not in {demos_dir}, fetching {watcher.demo_url(cand['demo_sha'])}")
    return Path(watcher.download_demo({"sha": cand["demo_sha"]}, demos_dir))


def run_worker(distro, worker_wsl, jobenv_wsl):
    """Run render_worker.sh in the distro, streaming its output live."""
    cmd = ["wsl", "-d", distro, "-e", "bash", worker_wsl, jobenv_wsl]
    print("+ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = []
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        print(line)
        output.append(line)
    return proc.wait(), output


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m clipsmith.render",
        description="clipsmith render orchestrator: candidate id -> raw mp4 on Drive",
    )
    ap.add_argument("candidate_id")
    ap.add_argument("--db", help="store DB path (default: state/candidates.db)")
    ap.add_argument("--demos-dir", default=str(REPO_ROOT / "state" / "demos"),
                    help="demo cache dir (default: state/demos)")
    ap.add_argument("--preroll", type=float, default=DEFAULT_PREROLL_S)
    ap.add_argument("--countdown", type=float, default=DEFAULT_COUNTDOWN_S,
                    help="demo-start -> match-start offset (KTX default 10)")
    ap.add_argument("--fps", type=int, default=DEFAULT_FPS)
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    ap.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    ap.add_argument("--display", type=int, default=DEFAULT_DISPLAY,
                    help="Xvfb display number inside the distro")
    ap.add_argument("--distro", default=DEFAULT_DISTRO)
    ap.add_argument("--staging-root", default=DEFAULT_STAGING_ROOT,
                    help="ext4 staging dir inside the distro")
    ap.add_argument("--engine-dir", default=DEFAULT_ENGINE_DIR)
    ap.add_argument("--base-content", default=DEFAULT_BASE_CONTENT,
                    help="content basedir inside the distro (hardlink-cloned per job)")
    ap.add_argument("--stage-dir", default=DEFAULT_STAGE_DIR)
    ap.add_argument("--final-dir", default=DEFAULT_FINAL_DIR)
    ap.add_argument("--map-source-dir", default=DEFAULT_MAP_SOURCE_DIR,
                    help="read-only Windows dir with loose .bsp files for custom maps")
    ap.add_argument("--hard-timeout", type=int, default=DEFAULT_HARD_TIMEOUT)
    ap.add_argument("--early-frame", type=int, default=DEFAULT_EARLY_FRAME)
    ap.add_argument("--keep-staging", action="store_true",
                    help="keep the in-distro job dir for debugging")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the render plan and idempotency decision; "
                         "write no files, launch no worker, and update no store status")
    ap.add_argument("--force-status", action="store_true",
                    help="render regardless of candidate status (testing); "
                         "the selected->rendered flip is skipped if illegal")
    args = ap.parse_args(argv)

    from clipsmith import store

    cand = store.get_candidate(args.candidate_id, db_path=args.db)
    print(f"candidate {cand['id']}: {cand['category']} — {cand['pov_player']['name']} "
          f"on {cand['map']} ({cand['demo_sha'][:12]}…), "
          f"window {cand['window']['start_s']}–{cand['window']['end_s']} "
          f"[status: {cand['status']}]")

    plan = plan_render(cand, preroll=args.preroll, countdown=args.countdown,
                       fps=args.fps)
    out_name = output_basename(cand)
    final = final_output_path(cand, args.final_dir)
    print(f"plan: demo_jump {plan['seek_s']} (match {cand['window']['start_s']}s "
          f"+ {args.countdown}s countdown - {args.preroll}s preroll), "
          f"track {plan['userid']}, capture {plan['capture_s']}s "
          f"({plan['expected_frames']} frames @ {args.width}x{args.height}) "
          f"-> {out_name}.mov")

    try:
        decision = render_decision(
            cand["status"], usable_raw_exists(final),
            force_status=args.force_status)
    except ValueError as e:
        sys.exit(f"error: {e}")

    if args.dry_run:
        would_update = decision == "skip-flip" or (
            decision == "run" and cand["status"] in ("selected", "revision_requested"))
        print(f"dry-run: decision={decision}; output={final}")
        print("dry-run: no demo download, job files, worker launch, file move, "
              "or store status update")
        print(json.dumps({
            "candidate": cand["id"],
            "dry_run": True,
            "decision": decision,
            "output": str(final),
            "would_update_status": "rendered" if would_update else None,
        }))
        return

    if decision == "skip":
        size = final.stat().st_size
        print(f"render: SKIPPED -- canonical raw already present: {final}")
        print(json.dumps({"candidate": cand["id"], "output": str(final),
                          "bytes": size, "status": cand["status"],
                          "skipped": True}))
        return

    if decision == "skip-flip":
        size = final.stat().st_size
        print(f"render: SKIPPED -- canonical raw already present: {final}")
        store.update_status(cand["id"], "rendered", db_path=args.db)
        print(f"store: {cand['id']} -> rendered "
              "(render skipped, raw already on Drive)")
        print(json.dumps({"candidate": cand["id"], "output": str(final),
                          "bytes": size, "status": "rendered",
                          "skipped": True}))
        return

    demo_path = ensure_demo(cand, args.demos_dir)

    # Job dir on the Windows side (under the stage dir): cfg pair + job.env
    # in, validation frame + blackdetect report out.
    stage_dir = Path(args.stage_dir)
    job_dir = stage_dir / f".job-{cand['id'][:8]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "clipsmith_arm.cfg").write_text(
        build_arm_cfg(DEMO_NAME, fps=args.fps, width=args.width,
                      height=args.height), newline="\n")
    (job_dir / "clipsmith_capgo.cfg").write_text(
        build_capgo_cfg(plan["seek_s"], plan["userid"], plan["capture_s"]),
        newline="\n")
    # The parity HUD layer (#20) ships with the job — the arm cfg exec's it.
    shutil.copy(SCRIPTS_RENDER_DIR / "cfg" / HUD_CFG_NAME,
                job_dir / HUD_CFG_NAME)

    map_src = Path(args.map_source_dir) / f"{cand['map']}.bsp"
    env = {
        "JOB_ID": cand["id"][:8],
        "DISPLAY_NUM": args.display,
        "WIDTH": args.width,
        "HEIGHT": args.height,
        "FPS": args.fps,
        "CAPTURE_S": plan["capture_s"],
        "EXPECTED_FRAMES": plan["expected_frames"],
        "EARLY_FRAME": args.early_frame,
        "DEMO_SRC": win_to_wsl(demo_path),
        "DEMO_NAME": DEMO_NAME,
        "CFG_DIR": win_to_wsl(job_dir),
        "MAP_SRC": win_to_wsl(map_src) if map_src.exists() else "",
        "MAP_NAME": cand["map"],
        "BASE_CONTENT": args.base_content,
        "ENGINE_DIR": args.engine_dir,
        "STAGING_ROOT": args.staging_root,
        "HARD_TIMEOUT": args.hard_timeout,
        "OUT_DIR": win_to_wsl(stage_dir),
        "OUT_NAME": out_name,
        "KEEP_STAGING": "1" if args.keep_staging else "0",
    }
    (job_dir / "job.env").write_text(build_job_env(env), newline="\n")

    worker_wsl = win_to_wsl(SCRIPTS_RENDER_DIR / "render_worker.sh")
    code, _ = run_worker(args.distro, worker_wsl, win_to_wsl(job_dir / "job.env"))
    if code != 0:
        record_render_failure(cand["id"], code,
                              f"render worker exit {code}", db_path=args.db)
        sys.exit(f"error: render worker failed with exit code {code} "
                 f"(job artifacts in {job_dir}; failure recorded in "
                 f"{FAILURES_LOG.name} — candidate stays 'selected')")

    staged = stage_dir / f"{out_name}.mov"
    if not staged.exists():
        record_render_failure(cand["id"], -1, "worker ok but staged mov missing",
                              db_path=args.db)
        sys.exit(f"error: worker reported success but {staged} is missing")

    blackdetect = (job_dir / "blackdetect.txt").read_text().strip() \
        if (job_dir / "blackdetect.txt").exists() else "(no report)"
    final_dir = Path(args.final_dir)
    final_dir.mkdir(parents=True, exist_ok=True)
    final = final_dir / staged.name
    if final.exists():
        final.unlink()
    shutil.move(str(staged), str(final))
    size = final.stat().st_size
    print(f"final: {final} ({size / 1e6:.1f} MB)")
    print(f"blackdetect: {blackdetect or 'none'}")

    try:
        store.update_status(cand["id"], "rendered", db_path=args.db)
        print(f"store: {cand['id']} -> rendered")
    except ValueError as e:
        if not args.force_status:
            raise
        print(f"store: status flip skipped under --force-status ({e})")

    print(json.dumps({"candidate": cand["id"], "output": str(final),
                      "bytes": size, "blackdetect": blackdetect or "none"}))


if __name__ == "__main__":
    main()
