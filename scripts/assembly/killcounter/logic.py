"""Pure timing/counter logic for the kill-counter overlay (issue #24).

No Playwright/ffmpeg in this module — everything here is unit-tested in
tests/test_killcounter.py. The rendering driver (generate.py) consumes the
schedule this module produces.

Decisions (documented per ticket):

- Scope: consumes ``candidate.metrics.kills[]`` events shaped
  ``{t_s, weapon, victim, teamkill}`` (QUAD RUNNER / PENT RUNNER /
  TEAM KILLER all emit this shape). PENT GRIND candidates instead carry a
  ``deaths[]`` ledger (killer/victim of BOTH teams' losses in the pent
  fight) — that is not the POV player's kill feed, so a kill counter does
  not apply; ``extract_kill_events`` raises a clear ValueError for it.

- Teamkill handling: a teamkill does NOT increment the counter (and does
  not decrement it — pipeline-spec says teamkills get an "oops"-flavored
  effect *instead* of a normal increment, not a penalty). The count is
  left unchanged and the oops treatment (red flash + comic wobble +
  "OOPS!" tag) plays on the chip. A window with only teamkills (TEAM
  KILLER candidates) therefore shows the chip stuck at 0 with oops
  effects — the joke lands by itself.

- Position: upper-right (chip centred at ``CHIP_CX``/``CHIP_CY``). In the
  hub-parity capture HUD the frag tracker sits top-left, team scores +
  game clock top-center, and the teaminfo panel is anchored left — the
  upper-right quadrant below the score banner is clear of game UI.

- Alignment: two modes (``overlay_window``):
  * ``window``:  t0 = window.start_s, duration = end_s - start_s.
    Overlay time for kill i is exactly ``t_s - window.start_s``.
  * ``capture``: mirrors clipsmith.render.plan_render so
    the overlay stacks 1:1 on the raw capture —
    ``seek = max(0, floor(start_s + countdown - preroll))``,
    ``t0 = seek - countdown`` (match-relative), ``duration = capture_s``.
    Kills inside the preroll land at positive offsets and are kept.
  Events before t0 or past the end are dropped (returned under
  ``"dropped"`` so the caller can log them).

- Simultaneous / near-simultaneous kills: increments are queued
  ``MIN_GAP_FRAMES`` apart so each number is visible on its own frame; at
  30 fps the worst-case shift is 2 frames = 67 ms, inside the 100 ms
  verification budget from the ticket. SFX offsets reuse the same
  frame-quantized times so audio transients align with the visual ticks
  exactly.
"""
import math
import re

DEFAULT_FPS = 30
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_PREROLL_S = 5.0     # matches clipsmith.render
DEFAULT_COUNTDOWN_S = 10.0  # KTX standard, matches hub_links / render plan

POP_FRAMES = 12             # kill pop anim length (0.4 s @30) — punchy
OOPS_FRAMES = 24            # oops anim length (0.8 s @30)
MIN_GAP_FRAMES = 2          # queue spacing for same-instant kills (67 ms @30)

# Chip anchor (canvas px, 1920x1080): upper-right, clear of the parity HUD.
CHIP_CX = 1700
CHIP_CY = 250

# Drop-in config point for real sounds later: PLACEHOLDER-named synthesized
# wavs live next to this module under sfx/ (see generate.py for synthesis).
SFX_FILES = {
    "kill": "PLACEHOLDER_kill_pop.wav",
    "oops": "PLACEHOLDER_oops.wav",
}
# Rising pitch per combo step keeps fast counting legible in audio.
SFX_PITCH_STEP = 0.05
SFX_PITCH_MAX = 1.35


# ----------------------------------------------------------- event intake

def extract_kill_events(candidate):
    """metrics.kills[] -> list of {t_s, weapon, victim, teamkill}.

    Raises ValueError for PENT GRIND-style deaths[]-only metrics (a deaths
    ledger of both teams is not a kill feed — out of scope, documented).
    An absent/empty kills[] list returns [] (zero-kill windows are legal).
    """
    metrics = candidate.get("metrics") or {}
    kills = metrics.get("kills")
    if kills is None:
        if "deaths" in metrics:
            raise ValueError(
                "candidate carries metrics.deaths[] (PENT GRIND death ledger), "
                "not metrics.kills[] — kill counter does not apply")
        return []
    out = []
    for k in kills:
        out.append({
            "t_s": float(k["t_s"]),
            "weapon": k.get("weapon"),
            "victim": k.get("victim"),
            "teamkill": bool(k.get("teamkill")),
        })
    return out


def overlay_window(candidate, mode="window", preroll=DEFAULT_PREROLL_S,
                   countdown=DEFAULT_COUNTDOWN_S):
    """Candidate window -> (t0, duration_s) in match-relative seconds.

    mode="window":  overlay covers exactly the candidate window.
    mode="capture": overlay covers the raw capture produced by
                    render_candidate.plan_render (same floor/ceil math),
                    so it stacks on the clip track 1:1.
    """
    start = float(candidate["window"]["start_s"])
    end = float(candidate["window"]["end_s"])
    if end <= start:
        raise ValueError(f"degenerate window: start_s={start} end_s={end}")
    if mode == "window":
        return start, end - start
    if mode == "capture":
        seek_s = max(0, math.floor(start + countdown - preroll))
        capture_s = math.ceil(end + countdown - seek_s)
        return seek_s - countdown, float(capture_s)
    raise ValueError(f"unknown alignment mode: {mode!r}")


# ----------------------------------------------------------- the timeline

def build_timeline(events, t0, duration_s, fps=DEFAULT_FPS,
                   min_gap_frames=MIN_GAP_FRAMES):
    """Kill events (match-relative) -> frame-exact overlay timeline.

    Returns a dict (JSON-serializable, consumed by build_overlay.mjs):
        fps, duration_s, total_frames,
        events: [{frame, type: kill|oops, count, t_s, offset_s,
                  victim, weapon, anim_end}],   # frame-sorted
        dropped: [{t_s, reason}]
    Counter semantics: kills increment, teamkills (type "oops") leave the
    count unchanged. offset_s is the frame-quantized overlay time (used by
    the SFX schedule too, so audio == visuals).
    """
    if duration_s <= 0:
        raise ValueError(f"duration_s must be positive: {duration_s}")
    total_frames = math.ceil(duration_s * fps - 1e-9)
    out, dropped = [], []
    count = 0
    prev_frame = None
    for ev in sorted(events, key=lambda e: float(e["t_s"])):
        t_s = float(ev["t_s"])
        offset = t_s - t0
        if offset < 0:
            dropped.append({"t_s": t_s, "reason": "before overlay t0"})
            continue
        frame = int(round(offset * fps))
        if prev_frame is not None and frame < prev_frame + min_gap_frames:
            frame = prev_frame + min_gap_frames   # queue same-instant pops
        if frame >= total_frames:
            dropped.append({"t_s": t_s, "reason": "past overlay end"})
            continue
        teamkill = bool(ev.get("teamkill"))
        if not teamkill:
            count += 1
        anim_len = OOPS_FRAMES if teamkill else POP_FRAMES
        out.append({
            "frame": frame,
            "type": "oops" if teamkill else "kill",
            "count": count,
            "t_s": t_s,
            "offset_s": frame / fps,
            "victim": ev.get("victim"),
            "weapon": ev.get("weapon"),
            "anim_end": min(frame + anim_len, total_frames - 1),
        })
        prev_frame = frame
    return {
        "fps": fps,
        "duration_s": duration_s,
        "total_frames": total_frames,
        "width": DEFAULT_WIDTH,
        "height": DEFAULT_HEIGHT,
        "chip": {"cx": CHIP_CX, "cy": CHIP_CY},
        "events": out,
        "dropped": dropped,
    }


def dynamic_frames(timeline):
    """Frames that need a fresh screenshot (animation active).

    Everything else is pixel-identical to its predecessor and is copied by
    render_frames.mjs instead of re-screenshotted. Frame 0 is always
    included (establishes the transparent base / first copy source), and so
    is one frame PAST each anim_end: the animation's final frame still
    renders the active treatment (e.g. the oops red border at t=1.0), so
    the settled state must be screenshotted to serve as the copy source
    for the idle span that follows.
    """
    frames = {0}
    for ev in timeline["events"]:
        frames.update(range(ev["frame"], ev["anim_end"] + 2))
    return sorted(f for f in frames if 0 <= f < timeline["total_frames"])


def sfx_schedule(timeline, pitch_step=SFX_PITCH_STEP, pitch_max=SFX_PITCH_MAX):
    """Timeline -> audio events for the stem mixer.

    [{offset_s, sound: kill|oops, pitch}] — offset_s is the frame-quantized
    visual time (transients align with the ticks); kill pitch rises with
    the combo count, oops plays at 1.0.
    """
    out = []
    for ev in timeline["events"]:
        if ev["type"] == "kill":
            pitch = min(1.0 + pitch_step * (ev["count"] - 1), pitch_max)
            out.append({"offset_s": ev["offset_s"], "sound": "kill",
                        "pitch": round(pitch, 4)})
        else:
            out.append({"offset_s": ev["offset_s"], "sound": "oops",
                        "pitch": 1.0})
    return out


# ----------------------------------------------------------- output naming

def slugify(text):
    """Lowercase, non-alphanumeric -> '-', collapsed (matches render naming)."""
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def output_basename(candidate):
    """Asset naming, parallel to the raw-capture convention:

    <matchdate>_<map>_<teams>_<pov+category>_killcounter
    e.g. 2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_killcounter
    """
    matchdate = re.sub(r"[^0-9-]", "", str(candidate["matchdate"])[:10])
    teams = "-vs-".join(slugify(t["name"]) for t in candidate["teams"])
    situation = slugify(f"{candidate['pov_player']['name']} {candidate['category']}")
    return f"{matchdate}_{slugify(candidate['map'])}_{teams}_{situation}_killcounter"
