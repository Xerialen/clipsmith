r"""clipsmith Resolve assembly driver (issue #25, P3.6).

Candidate id -> editable DaVinci Resolve project + assembled deliverable:

    C:\Users\example\Videos\clipsmith\branded\<stem>_assembled.mp4

Timeline per the pipeline-spec 6-element structure:

    V3:                       [kill-counter qtrle .mov, alpha]
    V2:                       [Demo Clip Focus frame, qtrle .mov @timeline fps]
    V1:  [intro][title card]  [raw clip, window range          ]  [outro=intro]
    A1:  [music bed: beatforge beat, full length, fade-out]
    A2:        [title audio]  [kill-counter SFX stem]
    A3:                       [game audio, ducked under the music]

Driven through Resolve's Python scripting API (DaVinciResolveScript /
fusionscript.dll) so it runs headless from one CLI invocation — the Resolve
MCP exposes the same operations and was used to prove the flow interactively.

Resolve MCP gotchas encoded here (docs/pipeline-spec.md inventory):
- `_mcp_` project-name prefix: the project is created WITH the prefix
  (`_mcp_clipsmith_<id8>`) so MCP tooling (which auto-prefixes) and this
  script address the same project; lookups try both spellings
  (logic.project_lookup_names).
- `require_temp_target:false`: only meaningful when rendering through the
  MCP render tool — this script renders directly to the final TargetDir,
  which is the same behavior. If you drive the render via MCP instead,
  pass require_temp_target=false.
- 150-frame still cap: originally handled by butt-joined still chunks
  (logic.still_chunks); superseded 2026-06-11 — stills place
  non-deterministically at fps != 30 (imported as 1-frame FPS-24 clips),
  so V2 is now a clip-length qtrle video (ensure_frame_overlay_video).
- Cold-launch Project Manager modal: detected (connection OK but project
  ops return None) and reported with exit code 3 — never silently hangs.

Picture settings: docs/picture-settings.md LOCKED rows — 1920x1080 timeline,
fps read from the clip (never retimed), Rec.709 (Scene)/Gamma 2.4 DaVinci
YRGB, delivery MP4 H.264 High @ 8000/12000 kbps, AAC-LC 384 kbps 48 kHz.

Usage:
    python scripts/assembly/resolve_assembly/assemble.py <candidate-id>
    ... [--db PATH] [--json cand.json]
        [--intro PATH] [--music PATH]
        [--raw-dir DIR] [--assets-dir DIR] [--out-dir DIR]
        [--launch] [--skip-render] [--keep-project]

Config drop-in points:
    --intro / DEFAULT_INTRO: assets/placeholder/intro.mp4 — replace with the
      real OneMoreQUAD intro (HITL #13) with no code change (auto-created
      from the candidate's title card if missing, so the build never blocks).
    --music: owner-chosen music; default auto-picks a beatforge beat.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))

import logic  # noqa: E402

# ------------------------------------------------------------------ defaults

OUTPUT_ROOT = Path(
    os.environ.get("CLIPSMITH_OUTPUT_ROOT", Path.home() / "Videos" / "clipsmith")
)
DEFAULT_RAW_DIR = Path(os.environ.get("CLIPSMITH_RAW_DIR", OUTPUT_ROOT / "raw"))
DEFAULT_ASSETS_DIR = Path(
    os.environ.get("CLIPSMITH_ASSETS_DIR", OUTPUT_ROOT / "assets" / "generated")
)
DEFAULT_OUT_DIR = Path(os.environ.get("CLIPSMITH_FINAL_DIR", OUTPUT_ROOT / "branded"))
DEFAULT_BEATFORGE_DIR = Path(
    os.environ.get("CLIPSMITH_BEATFORGE_DIR", OUTPUT_ROOT / "music")
)
DEFAULT_INTRO = REPO_ROOT / "assets" / "placeholder" / "intro.mp4"

BRANDING_DIR = REPO_ROOT / "scripts" / "branding"
_brand_scenes_dir = os.environ.get("CLIPSMITH_BRAND_SCENES_DIR")
BRAND_SCENES_DIR = Path(_brand_scenes_dir) if _brand_scenes_dir else None
BRAND_SCENE_FOCUS = (
    BRAND_SCENES_DIR / "QUAD RUNNER - Demo Clip Focus (1920x1080).html"
    if BRAND_SCENES_DIR else None
)
BRAND_FONT_SCENE = (
    BRAND_SCENES_DIR / "QUAD RUNNER - Starting Soon (1920x1080).html"
    if BRAND_SCENES_DIR else None
)
FONT_FILES = (
    "qrf_Nightrunner-700-normal.woff2",
    "qrf_ShareTechMono-400-normal.woff2",
)

RESOLVE_SCRIPT_API = Path(
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting")
RESOLVE_SCRIPT_LIB = Path(
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll")
RESOLVE_EXE = Path(
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe")

RENDER_POLL_S = 3
RENDER_TIMEOUT_S = 900
CONNECT_TIMEOUT_S = 90

EXIT_ARTIFACTS = 2
EXIT_RESOLVE = 3
EXIT_RENDER = 4

COLD_LAUNCH_MSG = (
    "RESOLVE_COLD_LAUNCH: connected to Resolve but project operations return "
    "None — this is the cold-launch Project Manager modal (findings: needs "
    "ONE human click per boot). Click the modal in the Resolve window, then "
    "re-run this command."
)


def log(msg):
    print(f"[assemble] {msg}", flush=True)


def run(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


# ------------------------------------------------------------------ probing

def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def probe_video(path):
    info = ffprobe(path)
    dur = float(info["format"]["duration"])
    vstreams = [s for s in info["streams"] if s["codec_type"] == "video"]
    if not vstreams:
        raise ValueError(f"no video stream in {path}")
    v = vstreams[0]
    num, den = v["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    # Normalize to the integer rate the locked pipeline produces (review on
    # PR #52): timeline math, generator --fps flags, and Resolve settings all
    # assume integer fps. Only float-division noise snaps; genuinely
    # fractional rates (59.94 NTSC etc.) are an ERROR — silently snapping
    # them onto an integer timeline is exactly the retime/drift this lock
    # exists to prevent.
    if abs(fps - round(fps)) <= 0.001:
        fps = int(round(fps))
    else:
        raise ValueError(
            f"{path}: fractional frame rate {fps:.3f} unsupported — the locked "
            f"pipeline produces integer fps (docs/picture-settings.md)")
    return {"duration_s": dur, "fps": fps,
            "width": int(v["width"]), "height": int(v["height"])}


# ------------------------------------------------------- asset preparation

def ensure_node_deps():
    if (HERE / "node_modules" / "playwright").exists():
        return
    run(["npm", "install", "--no-audit", "--no-fund"], cwd=HERE,
        shell=(sys.platform == "win32"))


def ensure_frame_overlay(cand, art, workdir):
    """Generate the Demo Clip Focus frame + patched lower-third alpha PNG
    (cached in assets/generated as <stem>_frame.png)."""
    out = art["frame_overlay"]
    if out.exists():
        log(f"frame overlay cached: {out.name}")
        return
    if BRAND_SCENE_FOCUS is None:
        sys.exit("error: set CLIPSMITH_BRAND_SCENES_DIR to the OBS scenes directory")
    if not BRAND_SCENE_FOCUS.exists():
        sys.exit(f"error: brand scene not found: {BRAND_SCENE_FOCUS}")
    ensure_node_deps()
    workdir.mkdir(parents=True, exist_ok=True)
    if not all((workdir / f).exists() for f in FONT_FILES):
        run(["node", BRANDING_DIR / "pick_fonts2.mjs", BRAND_FONT_SCENE, workdir])
    fields = logic.lowerthird_fields(cand)
    scene_png = workdir / "scene_alpha.png"
    lower_png = workdir / "lowerthird.png"
    cmd = ["node", HERE / "render_frame_overlay.mjs",
           "--scene", BRAND_SCENE_FOCUS, "--fonts", workdir,
           "--out-scene", scene_png, "--out-lower", lower_png,
           "--player", fields["player"], "--map", fields["map"]]
    if fields["clan"]:
        cmd += ["--clan", fields["clan"]]
    run(cmd)
    run(["ffmpeg", "-y", "-v", "error", "-i", scene_png, "-i", lower_png,
         "-filter_complex", "[0][1]overlay", "-frames:v", "1", out])
    log(f"frame overlay generated: {out.name}")


def ensure_frame_overlay_video(art, fps, dur_s):
    """Encode the static frame PNG as a qtrle+alpha video at timeline fps.

    Stills proved non-deterministic on the V2 track (2026-06-11, the
    BLooD_DoG 60 fps run): Resolve imports a PNG as a 1-frame clip with
    nominal FPS 24.0 and conforms AppendToTimeline's startFrame/endFrame
    through that rate — the butt-joined 150-frame chunks landed as a
    372/300/150 mix and overran the outro by 210 frames (+12 past the
    timeline end). At a 30 fps timeline the 5 s standard-still duration
    is exactly 150 frames, which is why the kippo validation passed.
    A constant-content qtrle video at the timeline fps uses the exact
    placement semantics already proven by the V3 kill-counter overlay.
    Always re-encoded (cheap, RLE) so a longer clip can't reuse a shorter
    cached overlay."""
    run(["ffmpeg", "-y", "-v", "error", "-loop", "1",
         "-framerate", fps, "-i", art["frame_overlay"],
         "-t", f"{dur_s:.3f}", "-c:v", "qtrle", "-pix_fmt", "argb",
         art["frame_overlay_video"]])
    log(f"frame overlay video: {art['frame_overlay_video'].name} "
        f"({dur_s:.3f}s @{fps})")


def ensure_intro(intro_path, art):
    """Intro placeholder (HITL #13): if the configured intro is missing,
    stand one in by copying the candidate's title card (3.5 s, branded)."""
    if intro_path.exists():
        return intro_path
    if not art["title_card"].exists():
        sys.exit(f"error: intro missing ({intro_path}) and no title card to "
                 f"derive a placeholder from ({art['title_card']})")
    intro_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(art["title_card"], intro_path)
    log(f"intro placeholder created from title card -> {intro_path} "
        "(replace with the real OneMoreQUAD intro, HITL #13)")
    return intro_path


def ensure_gameaudio(art, in_offset_s, dur_s):
    out = art["gameaudio"]
    if out.exists():
        return
    run(["ffmpeg", "-y", "-v", "error",
         "-ss", f"{in_offset_s:.3f}", "-t", f"{dur_s:.3f}", "-i", art["raw"],
         "-vn", "-ac", "2", "-ar", "48000",
         "-af", f"volume={logic.GAME_AUDIO_GAIN}",
         "-c:a", "pcm_s16le", out])
    log(f"game-audio stem: {out.name} (gain {logic.GAME_AUDIO_GAIN})")


def ensure_musicbed(art, music_src, total_s):
    out = art["musicbed"]
    if out.exists():
        return
    fade_start = max(0.0, total_s - logic.MUSIC_FADEOUT_S)
    run(["ffmpeg", "-y", "-v", "error",
         "-stream_loop", "-1", "-i", music_src,
         "-t", f"{total_s:.3f}", "-ac", "2", "-ar", "48000",
         "-af", (f"volume={logic.MUSIC_GAIN},"
                 f"afade=t=out:st={fade_start:.3f}:d={logic.MUSIC_FADEOUT_S}"),
         "-c:a", "pcm_s16le", out])
    log(f"music bed: {out.name} (source {Path(music_src).name})")


def pick_music_path(args_music, beatforge_dir, cand_id):
    if args_music:
        p = Path(args_music)
        if not p.exists():
            sys.exit(f"error: --music not found: {p}")
        return p
    names = [p.name for p in beatforge_dir.glob("*.wav")]
    pick = logic.pick_music(names, cand_id)
    if not pick:
        sys.exit(f"error: no beatforge beats in {beatforge_dir} and no --music")
    log(f"music auto-pick (beatforge): {pick}")
    return beatforge_dir / pick


# --------------------------------------------------------------- Resolve API

def connect_resolve(launch=False):
    """Connect via DaVinciResolveScript; optionally launch Resolve and wait.

    Returns the resolve app object, or exits 3 with a clear message
    (external scripting off / not running / cold-launch modal)."""
    sys.path.insert(0, str(RESOLVE_SCRIPT_API / "Modules"))
    try:
        import DaVinciResolveScript as dvr
    except ImportError as e:
        sys.exit(f"error: DaVinciResolveScript module not found "
                 f"({RESOLVE_SCRIPT_API / 'Modules'}): {e}")

    resolve = dvr.scriptapp("Resolve")
    if resolve is None and launch:
        if not RESOLVE_EXE.exists():
            sys.exit(f"error: Resolve.exe not found: {RESOLVE_EXE}")
        log("launching Resolve...")
        subprocess.Popen([str(RESOLVE_EXE)])
        deadline = time.time() + CONNECT_TIMEOUT_S
        while resolve is None and time.time() < deadline:
            time.sleep(3)
            resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        print("RESOLVE_UNAVAILABLE: could not connect. Either Resolve is not "
              "running (use --launch), or Preferences > System > General > "
              "'External scripting using' is not set to Local.", file=sys.stderr)
        sys.exit(EXIT_RESOLVE)
    return resolve


def get_project_manager(resolve):
    pm = resolve.GetProjectManager()
    if pm is None:
        print(COLD_LAUNCH_MSG, file=sys.stderr)
        sys.exit(EXIT_RESOLVE)
    return pm


def fresh_project(pm, name, keep_existing=False):
    """Create the per-clip project, handling the `_mcp_` prefix on lookup
    and replacing a stale copy unless --keep-project."""
    existing = set(pm.GetProjectListInCurrentFolder() or [])
    for candidate_name in logic.project_lookup_names(name):
        if candidate_name in existing:
            if keep_existing:
                proj = pm.LoadProject(candidate_name)
                if proj is None:
                    print(COLD_LAUNCH_MSG, file=sys.stderr)
                    sys.exit(EXIT_RESOLVE)
                log(f"reusing existing project: {candidate_name}")
                return proj, candidate_name
            cur = pm.GetCurrentProject()
            if cur and cur.GetName() == candidate_name:
                pm.CreateProject(f"__scratch_{int(time.time())}")
            if not pm.DeleteProject(candidate_name):
                log(f"warn: could not delete stale project {candidate_name}")
            else:
                log(f"deleted stale project: {candidate_name}")
    proj = pm.CreateProject(name)
    if proj is None:
        print(COLD_LAUNCH_MSG, file=sys.stderr)
        sys.exit(EXIT_RESOLVE)
    log(f"created project: {name}")
    return proj, name


def set_picture_settings(proj, fps):
    """docs/picture-settings.md LOCKED timeline rows. Set BEFORE creating
    the timeline so it inherits them."""
    wanted = {
        "timelineResolutionWidth": str(logic.TIMELINE_WIDTH),
        "timelineResolutionHeight": str(logic.TIMELINE_HEIGHT),
        "timelineFrameRate": str(int(fps) if float(fps).is_integer() else fps),
        "colorScienceMode": "davinciYRGB",
    }
    for k, v in wanted.items():
        if not proj.SetSetting(k, v):
            log(f"warn: project setting rejected: {k}={v}")
    # Rec.709 (Scene) / Gamma 2.4 — value spelling varies across versions.
    for cs in ("Rec.709 (Scene)", "Rec.709 Gamma 2.4"):
        if proj.SetSetting("timelineColorSpace", cs):
            log(f"timeline color space: {cs}")
            break
    else:
        log("warn: timelineColorSpace rejected (leaving project default)")


def import_media(media_pool, paths):
    """ImportMedia -> {resolved file path: MediaPoolItem}."""
    str_paths = [str(p) for p in paths]
    items = media_pool.ImportMedia(str_paths) or []
    by_path = {}
    for it in items:
        fp = it.GetClipProperty("File Path")
        if fp:
            by_path[str(Path(fp))] = it
    missing = [p for p in str_paths if str(Path(p)) not in by_path]
    if missing:
        sys.exit(f"error: Resolve failed to import: {missing}")
    return by_path


def append(media_pool, info):
    items = media_pool.AppendToTimeline([info])
    if not items or items[0] is None:
        sys.exit(f"error: AppendToTimeline failed for {info}")
    return items[0]


def build_timeline(proj, media_pool, mpi, plan, placements):
    """Build the 6-element timeline. `mpi` maps role -> MediaPoolItem.

    Strategy: V1 segments are appended sequentially (no recordFrame) and
    their actual positions read back — this self-corrects any endFrame
    inclusive/exclusive convention — then overlays/stems are placed at the
    measured positions via recordFrame."""
    tl = media_pool.CreateEmptyTimeline("assembly")
    if tl is None:
        sys.exit("error: CreateEmptyTimeline failed")
    while tl.GetTrackCount("video") < 3:
        tl.AddTrack("video")
    while tl.GetTrackCount("audio") < 3:
        tl.AddTrack("audio", "stereo")

    segs = plan["segments"]
    v1 = {}
    for name in plan["order"]:
        seg = segs[name]
        role = {"intro": "intro", "title": "title_card",
                "clip": "raw", "outro": "intro"}[name]
        src_in = placements["clip_in_frame"] if name == "clip" else 0
        item = append(media_pool, {
            "mediaPoolItem": mpi[role],
            "startFrame": src_in,
            "endFrame": src_in + seg["frames"],  # convention checked below
            "trackIndex": 1,
            "mediaType": 1,
        })
        v1[name] = item
        got = item.GetDuration()
        if got != seg["frames"]:
            log(f"warn: V1 {name} duration {got} != planned {seg['frames']} "
                "(endFrame convention) — overlay positions use measured starts")

    start_tc_frame = tl.GetStartFrame()  # timeline start timecode offset
    clip_start = v1["clip"].GetStart()
    title_start = v1["title"].GetStart()
    log(f"timeline start frame {start_tc_frame}; measured clip start {clip_start}")

    # V2: brand frame overlay — a qtrle+alpha video at timeline fps (NOT a
    # still: still placement is non-deterministic, see
    # ensure_frame_overlay_video; same proven mechanics as V3 below).
    clip_frames = v1["clip"].GetDuration()
    append(media_pool, {
        "mediaPoolItem": mpi["frame_overlay_video"],
        "startFrame": 0,
        "endFrame": clip_frames,
        "trackIndex": 2,
        "recordFrame": clip_start,
        "mediaType": 1,
    })

    # V3: kill-counter overlay (qtrle alpha), aligned with the clip start
    # (counter t0 == window.start_s == clip in-point, mode=window).
    append(media_pool, {
        "mediaPoolItem": mpi["killcounter"],
        "startFrame": 0,
        "endFrame": clip_frames,
        "trackIndex": 3,
        "recordFrame": clip_start,
        "mediaType": 1,
    })

    # A1: music bed under everything; A2: title + counter stems; A3: game audio.
    append(media_pool, {"mediaPoolItem": mpi["musicbed"], "startFrame": 0,
                        "endFrame": plan["total_frames"], "trackIndex": 1,
                        "recordFrame": start_tc_frame, "mediaType": 2})
    append(media_pool, {"mediaPoolItem": mpi["title_audio"], "startFrame": 0,
                        "endFrame": segs["title"]["frames"], "trackIndex": 2,
                        "recordFrame": title_start, "mediaType": 2})
    append(media_pool, {"mediaPoolItem": mpi["killcounter_sfx"], "startFrame": 0,
                        "endFrame": clip_frames, "trackIndex": 2,
                        "recordFrame": clip_start, "mediaType": 2})
    append(media_pool, {"mediaPoolItem": mpi["gameaudio"], "startFrame": 0,
                        "endFrame": clip_frames, "trackIndex": 3,
                        "recordFrame": clip_start, "mediaType": 2})

    built = tl.GetEndFrame() - tl.GetStartFrame()
    log(f"timeline built: {built} frames (planned {plan['total_frames']})")
    return tl


def render(proj, out_dir, out_name, delivery, skip=False):
    """Queue + run the delivery render per the LOCKED preset values.

    Direct-to-target (the scripting-API equivalent of the MCP's
    require_temp_target:false). GOP/B-frame/profile details not exposed by
    SetRenderSettings are attempted as optional keys and logged if rejected.
    """
    if not proj.SetCurrentRenderFormatAndCodec(delivery["format"],
                                               delivery["video_codec"]):
        log("warn: SetCurrentRenderFormatAndCodec rejected "
            f"({delivery['format']}/{delivery['video_codec']})")
    core = {
        "SelectAllFrames": True,
        "TargetDir": str(out_dir),
        "CustomName": out_name,
        "ExportVideo": True,
        "ExportAudio": True,
        "FormatWidth": delivery["width"],
        "FormatHeight": delivery["height"],
        "FrameRate": float(delivery["fps"]),
    }
    if not proj.SetRenderSettings(core):
        sys.exit("error: SetRenderSettings rejected the core delivery settings")
    # Resolve 21 API gaps (probed live, both rejected for every mp4 codec):
    # - "VideoQuality" (restrict-bitrate): not scriptable -> quality stays
    #   Automatic (came out ~30 Mbps on the kippo clip — ABOVE the locked
    #   8000/12000 kbps target, which only costs upload time per the
    #   picture-settings rationale; verify with ffprobe per render).
    # - AAC bitrate ("AudioBitRate"/variants): not scriptable -> Resolve
    #   default 320 kbps vs the locked 384 kbps.
    # GOP/B-frame/closed-GOP details are likewise not exposed.
    for k, v in (("VideoQuality", delivery["bitrate_kbps"]),
                 ("AudioCodec", delivery["audio_codec"]),
                 ("AudioBitDepth", 16),
                 ("AudioSampleRate", delivery["audio_sample_rate"]),
                 ("EncodingProfile", delivery["h264_profile"]),
                 ("NetworkOptimization", True)):
        if not proj.SetRenderSettings({k: v}):
            log(f"warn: optional render setting rejected: {k}={v}")
    if skip:
        log("--skip-render: render settings applied, job not queued")
        return None
    job = proj.AddRenderJob()
    if not job:
        sys.exit("error: AddRenderJob failed")
    if not proj.StartRendering(job):
        sys.exit("error: StartRendering failed")
    log(f"rendering job {job} ...")
    deadline = time.time() + RENDER_TIMEOUT_S
    while proj.IsRenderingInProgress():
        if time.time() > deadline:
            proj.StopRendering()
            print(f"RENDER_TIMEOUT after {RENDER_TIMEOUT_S}s", file=sys.stderr)
            sys.exit(EXIT_RENDER)
        time.sleep(RENDER_POLL_S)
    status = proj.GetRenderJobStatus(job) or {}
    if status.get("JobStatus") != "Complete":
        print(f"RENDER_FAILED: {status}", file=sys.stderr)
        sys.exit(EXIT_RENDER)
    log(f"render complete: {status}")
    return status


# --------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="clipsmith Resolve assembly: candidate id -> branded "
                    "_assembled.mp4 + editable _mcp_ project")
    ap.add_argument("candidate_id", nargs="?", help="candidate id in the store")
    ap.add_argument("--json", help="raw candidate JSON file (bypasses store)")
    ap.add_argument("--db", help="store DB path (default: state/candidates.db)")
    ap.add_argument("--intro", default=str(DEFAULT_INTRO),
                    help="intro/outro video (config drop-in for HITL #13; "
                         "placeholder auto-derived from the title card)")
    ap.add_argument("--music", help="owner-chosen music (default: beatforge auto-pick)")
    ap.add_argument("--beatforge-dir", default=str(DEFAULT_BEATFORGE_DIR))
    ap.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    ap.add_argument("--assets-dir", default=str(DEFAULT_ASSETS_DIR))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--workdir", help="scratch dir for overlay rendering "
                                      "(default: <assets-dir>/_overlay_work)")
    ap.add_argument("--launch", action="store_true",
                    help="launch Resolve if it is not running")
    ap.add_argument("--keep-project", action="store_true",
                    help="reuse an existing _mcp_ project instead of replacing it")
    ap.add_argument("--skip-render", action="store_true",
                    help="build project + timeline but do not render")
    args = ap.parse_args(argv)

    if args.json:
        cand = json.loads(Path(args.json).read_text(encoding="utf-8"))
    elif args.candidate_id:
        from clipsmith import store
        cand = store.get_candidate(args.candidate_id, db_path=args.db)
    else:
        ap.error("need a candidate id or --json")

    raw_dir, assets_dir = Path(args.raw_dir), Path(args.assets_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    art = logic.resolve_artifacts(cand, raw_dir, assets_dir, out_dir)

    if art["raw"] is None:
        print(f"error: no raw capture for candidate (tried "
              f"{logic.raw_capture_names(cand)} in {raw_dir})", file=sys.stderr)
        sys.exit(EXIT_ARTIFACTS)
    for role in ("title_card", "title_audio", "killcounter", "killcounter_sfx"):
        if not art[role].exists():
            print(f"error: missing generated asset {role}: {art[role]} "
                  "(run the P3.4/P3.5 generators first)", file=sys.stderr)
            sys.exit(EXIT_ARTIFACTS)

    # ---- plan ------------------------------------------------------------
    raw_info = probe_video(art["raw"])
    fps = raw_info["fps"]  # LOCKED: timeline fps = clip fps, never retime
    intro_path = ensure_intro(Path(args.intro), art)
    intro_info = probe_video(intro_path)
    title_info = probe_video(art["title_card"])

    in_offset = logic.clip_in_offset_s(cand)
    clip_s = logic.window_duration_s(cand)
    avail = raw_info["duration_s"] - in_offset
    if avail <= 0:
        print(f"error: raw capture too short: in-offset {in_offset:.3f}s "
              f">= duration {raw_info['duration_s']:.3f}s", file=sys.stderr)
        sys.exit(EXIT_ARTIFACTS)
    if avail < clip_s:
        log(f"warn: raw shorter than window, trimming clip to {avail:.3f}s")
        clip_s = avail

    plan = logic.plan_timeline(intro_info["duration_s"], title_info["duration_s"],
                               clip_s, intro_info["duration_s"], fps)
    log(f"plan: fps={fps:g} total={plan['total_frames']}f "
        f"({plan['total_s']:.2f}s) clip-in-offset={in_offset:.3f}s")

    # ---- derived media ---------------------------------------------------
    workdir = Path(args.workdir) if args.workdir else assets_dir / "_overlay_work"
    ensure_frame_overlay(cand, art, workdir)
    ensure_frame_overlay_video(art, fps, clip_s)
    ensure_gameaudio(art, in_offset, clip_s)
    music_src = pick_music_path(args.music, Path(args.beatforge_dir), cand["id"])
    ensure_musicbed(art, music_src, plan["total_s"])

    # ---- Resolve ---------------------------------------------------------
    resolve = connect_resolve(launch=args.launch)
    pm = get_project_manager(resolve)
    pname = logic.project_name(cand["id"])
    proj, pname = fresh_project(pm, pname, keep_existing=args.keep_project)
    set_picture_settings(proj, fps)

    media_pool = proj.GetMediaPool()
    roles = {
        "intro": intro_path,
        "title_card": art["title_card"],
        "title_audio": art["title_audio"],
        "raw": art["raw"],
        "killcounter": art["killcounter"],
        "killcounter_sfx": art["killcounter_sfx"],
        "frame_overlay_video": art["frame_overlay_video"],
        "gameaudio": art["gameaudio"],
        "musicbed": art["musicbed"],
    }
    by_path = import_media(media_pool, set(roles.values()))
    mpi = {role: by_path[str(Path(p))] for role, p in roles.items()}

    placements = {"clip_in_frame": round(in_offset * fps)}
    build_timeline(proj, media_pool, mpi, plan, placements)

    if not pm.SaveProject():
        log("warn: SaveProject returned false")

    out_stem = art["output"].stem
    delivery = logic.delivery_settings(fps)
    render(proj, out_dir, out_stem, delivery, skip=args.skip_render)
    pm.SaveProject()

    result = {
        "project": pname,
        "output": str(art["output"]),
        "output_exists": art["output"].exists(),
        "plan": {k: plan[k] for k in ("total_frames", "total_s", "fps")},
        "music": str(music_src),
        "intro": str(intro_path),
    }
    print(json.dumps(result, indent=2))
    if not args.skip_render and not result["output_exists"]:
        print("error: render reported complete but output missing",
              file=sys.stderr)
        sys.exit(EXIT_RENDER)
    return result


if __name__ == "__main__":
    main()
