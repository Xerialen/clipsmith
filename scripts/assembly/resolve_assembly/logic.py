r"""Pure logic for the Resolve assembly driver (issue #25, P3.6).

Everything in here is unit-testable with no Resolve, no ffmpeg, no
filesystem side effects (path *resolution* takes the directory listings /
Path objects it needs as arguments). The Resolve/ffmpeg driving lives in
assemble.py.

Covers:
- artifact path resolution by the established naming conventions
  (raw capture, title card, kill-counter overlay pair, generated frame
  overlay, derived audio stems, assembled output);
- timeline math: segment offsets for the 6-element spec structure
  (intro -> title card -> framed clip + counter overlay -> outro=intro),
  clip in-point alignment against the canonical render plan
  (clipsmith.render.plan_render math), frame quantization;
- the Resolve MCP gotchas that are *logic*: `_mcp_` project-name prefix
  handling and the 150-frame still cap (butt-join chunking);
- beatforge music auto-pick (deterministic per candidate id);
- delivery settings derived from the locked picture-settings doc
  (docs/picture-settings.md — timeline + delivery rows are LOCKED).
"""
import math
import re

# ---------------------------------------------------------------- constants

# Render-plan constants — MUST match clipsmith/render.py and
# scripts/assembly/killcounter/logic.py (same floor/ceil alignment math).
DEFAULT_PREROLL_S = 5.0
DEFAULT_COUNTDOWN_S = 10.0

# Resolve MCP gotchas (docs/pipeline-spec.md "Reusable components inventory").
MCP_PROJECT_PREFIX = "_mcp_"
STILL_FRAME_CAP = 150  # still images cap at 150 frames per timeline item

# Picture settings (docs/picture-settings.md — LOCKED rows).
TIMELINE_WIDTH = 1920
TIMELINE_HEIGHT = 1080
# Timeline fps is read from the clip — never let Resolve retime (LOCKED).

# Delivery (LOCKED): MP4 / H.264 High profile, AAC-LC 384 kbps 48 kHz stereo,
# 12,000 kbps at >=50 fps else 8,000 kbps; keyframe interval = fps/2,
# closed GOP, 2 B-frames.
DELIVERY_BITRATE_HIGHFPS_KBPS = 12000
DELIVERY_BITRATE_LOWFPS_KBPS = 8000
DELIVERY_AUDIO_BITRATE_KBPS = 384
DELIVERY_AUDIO_SAMPLE_RATE = 48000

# Audio plan gains (music dominant, game audio audible underneath — spec:
# "Game audio stays present under the music").
MUSIC_GAIN = 0.71      # ~ -3 dB
GAME_AUDIO_GAIN = 0.45  # ~ -7 dB
MUSIC_FADEOUT_S = 1.5


# ----------------------------------------------------------------- naming

def slugify(text):
    """Lowercase, non-alphanumeric -> '-', collapsed: 'kippo (FU)' -> 'kippo-fu'.

    Identical to clipsmith.render.slugify (the naming source).
    """
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def asset_stem(cand):
    """Shared asset stem: <matchdate>_<map>_<teams>_<pov+category>.

    e.g. 2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner
    (= the raw-capture basename without the _1pov[_raw] tail; the
    kill-counter pair appends _killcounter, we append _frame/_assembled etc.)
    """
    matchdate = re.sub(r"[^0-9-]", "", str(cand["matchdate"])[:10])
    teams = "-vs-".join(slugify(t["name"]) for t in cand["teams"])
    situation = slugify(f"{cand['pov_player']['name']} {cand['category']}")
    return f"{matchdate}_{slugify(cand['map'])}_{teams}_{situation}"


def raw_capture_names(cand):
    """Raw-capture filenames to try, in priority order.

    picture-settings.md locks new raw files to *_<cid8>_1pov_raw.mov.
    Older orchestrator output omitted the candidate id and/or used .mp4, so
    those names stay as fallbacks for already-rendered clips.
    """
    stem = asset_stem(cand)
    cid = slugify(str(cand["id"])[:8])
    return (
        f"{stem}_{cid}_1pov_raw.mov",
        f"{stem}_1pov_raw.mov",
        f"{stem}_1pov_raw.mp4",
        f"{stem}_1pov.mov",
        f"{stem}_1pov.mp4",
    )


def week_number(matchdate):
    """ISO week of the match date: '2026-06-08' -> 24."""
    import datetime
    y, m, d = (int(p) for p in str(matchdate)[:10].split("-"))
    return datetime.date(y, m, d).isocalendar()[1]


def title_basename(cand):
    """Title-card naming (scripts/assembly/title_card/make_title_card.py):
    week{NN}_{category}_{player} -> week24_quad-runner_kippo-fu
    """
    week = week_number(cand["matchdate"])
    return (
        f"week{week:02d}_{slugify(cand['category'])}"
        f"_{slugify(cand['pov_player']['name'])}"
    )


def killcounter_basename(cand):
    return f"{asset_stem(cand)}_killcounter"


def frame_overlay_name(cand):
    """Generated Demo Clip Focus frame + patched lower-third (alpha PNG)."""
    return f"{asset_stem(cand)}_frame.png"


def frame_overlay_video_name(cand):
    """The frame PNG encoded as a clip-length qtrle+alpha video at timeline
    fps — what actually goes on V2 (still placement in Resolve is
    non-deterministic; see assemble.ensure_frame_overlay_video)."""
    return f"{asset_stem(cand)}_frame.mov"


def gameaudio_name(cand):
    return f"{asset_stem(cand)}_gameaudio.wav"


def musicbed_name(cand):
    return f"{asset_stem(cand)}_musicbed.wav"


def output_name(cand):
    """Assembled deliverable: established naming + _assembled suffix."""
    return f"{asset_stem(cand)}_assembled.mp4"


def resolve_artifacts(cand, raw_dir, assets_dir, out_dir):
    """Locate the candidate's artifacts by naming convention.

    Takes Path-like dirs; returns a dict of paths. `raw` is the first
    existing raw-capture variant or None; generated inputs (title card,
    counter pair) are returned as the conventional paths whether or not
    they exist yet — assemble.py checks existence and errors helpfully.
    """
    raw = None
    for name in raw_capture_names(cand):
        p = raw_dir / name
        if p.exists():
            raw = p
            break
    title = title_basename(cand)
    kc = killcounter_basename(cand)
    return {
        "raw": raw,
        "title_card": assets_dir / f"{title}.mp4",
        "title_audio": assets_dir / f"{title}.wav",
        "killcounter": assets_dir / f"{kc}.mov",
        "killcounter_sfx": assets_dir / f"{kc}_sfx.wav",
        "frame_overlay": assets_dir / frame_overlay_name(cand),
        "frame_overlay_video": assets_dir / frame_overlay_video_name(cand),
        "gameaudio": assets_dir / gameaudio_name(cand),
        "musicbed": assets_dir / musicbed_name(cand),
        "output": out_dir / output_name(cand),
    }


# ----------------------------------------------------- lower-third / player

def parse_player(name):
    """'kippo (FU)' -> ('KIPPO', 'FU'); 'Milton' -> ('MILTON', None)."""
    m = re.match(r"^\s*(.*?)\s*[(\[]([^)\]]+)[)\]]\s*$", name)
    if m:
        return m.group(1).strip().upper(), m.group(2).strip().upper()
    return name.strip().upper(), None


def lowerthird_fields(cand):
    """Player/clan/map fields for the patched lower-third (player · map)."""
    player, clan = parse_player(cand["pov_player"]["name"])
    return {"player": player, "clan": clan, "map": str(cand["map"]).upper()}


# ------------------------------------------------------------ clip alignment

def window_duration_s(cand):
    start = float(cand["window"]["start_s"])
    end = float(cand["window"]["end_s"])
    if end <= start:
        raise ValueError(f"degenerate window: start_s={start} end_s={end}")
    return end - start


def clip_in_offset_s(cand, preroll=DEFAULT_PREROLL_S, countdown=DEFAULT_COUNTDOWN_S):
    """Offset of window.start_s into the raw capture, in seconds.

    Mirrors clipsmith.render.plan_render:
      seek_s = max(0, floor(start + countdown - preroll))   (demo-relative)
      capture t0 (match-relative) = seek_s - countdown
    so the window starts at  start - (seek_s - countdown)  into the capture.
    The kill-counter overlay is generated in mode=window (t0 = window.start_s),
    so placing the clip's [offset, offset+window] range and the counter at the
    same timeline position aligns them exactly.
    """
    start = float(cand["window"]["start_s"])
    end = float(cand["window"]["end_s"])
    if end <= start:
        raise ValueError(f"degenerate window: start_s={start} end_s={end}")
    seek_s = max(0, math.floor(start + countdown - preroll))
    return start - (seek_s - countdown)


# ------------------------------------------------------------- timeline math

def plan_timeline(intro_s, title_s, clip_s, outro_s, fps):
    """6-element structure -> frame-quantized segment plan.

    Segments on V1: intro | title | clip | outro (outro = intro reused).
    Returns offsets relative to timeline frame 0 (the caller adds the
    timeline's start-timecode frame). Frame counts are round(d * fps).
    """
    if fps <= 0:
        raise ValueError(f"bad fps: {fps}")
    durations = [
        ("intro", intro_s),
        ("title", title_s),
        ("clip", clip_s),
        ("outro", outro_s),
    ]
    segments = []
    cursor = 0
    for name, dur_s in durations:
        frames = round(dur_s * fps)
        if frames <= 0:
            raise ValueError(f"degenerate segment {name}: {dur_s}s @ {fps}fps")
        segments.append({"name": name, "offset_frames": cursor, "frames": frames})
        cursor += frames
    return {
        "fps": fps,
        "segments": {s["name"]: s for s in segments},
        "order": [s["name"] for s in segments],
        "total_frames": cursor,
        "total_s": cursor / fps,
    }


def still_chunks(frames, cap=STILL_FRAME_CAP):
    """Butt-join chunking for the 150-frame still cap (MCP gotcha).

    1050 -> [150]*7;  310 -> [150, 150, 10];  10 -> [10].
    """
    if frames <= 0:
        raise ValueError(f"degenerate still duration: {frames}")
    full, rem = divmod(frames, cap)
    return [cap] * full + ([rem] if rem else [])


# ----------------------------------------------------------------- music

def pick_music(filenames, cand_id):
    """Deterministic beatforge auto-pick: beat_*.wav only (no parody_*, no
    beat_ref_*), choice seeded by candidate id so each clip gets a stable
    beat but different clips vary. Returns the chosen filename or None.
    """
    beats = sorted(
        f for f in filenames
        if re.fullmatch(r"beat_\d+\.wav", f, flags=re.IGNORECASE)
    )
    if not beats:
        return None
    return beats[int(cand_id[:8], 16) % len(beats)]


# ----------------------------------------------------------- Resolve naming

def project_name(cand_id):
    """Editable per-clip project: _mcp_clipsmith_<candidateid8>.

    The `_mcp_` prefix is carried explicitly so the project is addressable
    both from the raw scripting API (full name) and through the Resolve MCP
    (which auto-prefixes `_mcp_` — see strip_mcp_prefix / lookup_names).
    """
    return f"{MCP_PROJECT_PREFIX}clipsmith_{cand_id[:8]}"


def ensure_mcp_prefix(name):
    return name if name.startswith(MCP_PROJECT_PREFIX) else MCP_PROJECT_PREFIX + name


def strip_mcp_prefix(name):
    return name[len(MCP_PROJECT_PREFIX):] if name.startswith(MCP_PROJECT_PREFIX) else name


def project_lookup_names(name):
    """Names to try when looking a project up (MCP `_mcp_` prefix gotcha):
    the exact name first, then the prefixed/stripped variant.
    """
    other = strip_mcp_prefix(name) if name.startswith(MCP_PROJECT_PREFIX) \
        else ensure_mcp_prefix(name)
    return (name, other)


# -------------------------------------------------------------- delivery

def delivery_settings(fps):
    """Locked delivery rows (docs/picture-settings.md) -> render settings.

    12,000 kbps at 1080p>=50fps, 8,000 kbps below; keyframe interval =
    half the frame rate, closed GOP, 2 B-frames, H.264 High profile,
    AAC-LC 384 kbps 48 kHz stereo.
    """
    if fps <= 0:
        raise ValueError(f"bad fps: {fps}")
    bitrate = (
        DELIVERY_BITRATE_HIGHFPS_KBPS if fps >= 50
        else DELIVERY_BITRATE_LOWFPS_KBPS
    )
    return {
        "format": "mp4",
        "video_codec": "H264",
        "width": TIMELINE_WIDTH,
        "height": TIMELINE_HEIGHT,
        "fps": fps,
        "bitrate_kbps": bitrate,
        "keyframe_interval": round(fps / 2),
        "closed_gop": True,
        "b_frames": 2,
        "h264_profile": "High",
        "audio_codec": "aac",
        "audio_bitrate_kbps": DELIVERY_AUDIO_BITRATE_KBPS,
        "audio_sample_rate": DELIVERY_AUDIO_SAMPLE_RATE,
    }
