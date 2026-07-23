"""Tests for scripts/assembly/killcounter/logic.py — pure logic only.

No Playwright/ffmpeg here (those live in generate.py / the .mjs helpers and
are validated live against real candidates). These tests pin the timestamp
mapping (window + capture alignment), the counter sequence incl. teamkill
handling, the animation keyframe schedule, the dynamic-frame optimization,
the SFX schedule, and the output naming.
"""
import importlib.util
import math
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KC_DIR = REPO_ROOT / "scripts" / "assembly" / "killcounter"

spec = importlib.util.spec_from_file_location("kc_logic", KC_DIR / "logic.py")
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)


# Real store shapes, abridged: the kippo QUAD RUNNER and bogojoker
# TEAM KILLER candidates used for the live validation.
def kippo_candidate():
    return {
        "id": "43f8ffef8bb4463a96c85afb7767ffd4",
        "category": "QUAD RUNNER",
        "map": "dm3",
        "matchdate": "2026-06-08",
        "teams": [{"name": "-fu-", "score": 210}, {"name": "mix", "score": 94}],
        "pov_player": {"name": "kippo (FU)", "userid": 58},
        "window": {"start_s": 726.269, "end_s": 761.269},
        "metrics": {"kills": [
            {"t_s": 731.35, "weapon": "lg", "victim": "paniagua", "teamkill": False},
            {"t_s": 741.222, "weapon": "lg", "victim": "paniagua", "teamkill": False},
            {"t_s": 744.956, "weapon": "lg", "victim": "Anza  (FU)", "teamkill": False},
            {"t_s": 751.998, "weapon": "lg", "victim": "Anza  (FU)", "teamkill": False},
            {"t_s": 752.558, "weapon": "lg", "victim": "paniagua", "teamkill": False},
            {"t_s": 756.953, "weapon": "lg", "victim": "paniagua", "teamkill": False},
            {"t_s": 758.628, "weapon": "rl", "victim": "Hto   (FU)", "teamkill": False},
        ]},
    }


def bogojoker_candidate():
    return {
        "id": "b7399132c3314ef2ac4bbc33aebe9125",
        "category": "TEAM KILLER",
        "map": "schloss",
        "matchdate": "2026-06-10",
        "teams": [{"name": "red", "score": 220}, {"name": "blue", "score": 194}],
        "pov_player": {"name": "bogojoker", "userid": 89},
        "window": {"start_s": 781.2, "end_s": 811.2},
        "metrics": {"kills": [
            {"t_s": 795.0310000000001, "weapon": None, "victim": "Venator", "teamkill": True},
            {"t_s": 797.369, "weapon": None, "victim": "Venator", "teamkill": True},
        ]},
    }


# ------------------------------------------------------ extract_kill_events

def test_extract_kills_passthrough():
    evs = logic.extract_kill_events(kippo_candidate())
    assert len(evs) == 7
    assert evs[0] == {"t_s": 731.35, "weapon": "lg", "victim": "paniagua",
                      "teamkill": False}


def test_extract_teamkill_flag_preserved():
    evs = logic.extract_kill_events(bogojoker_candidate())
    assert [e["teamkill"] for e in evs] == [True, True]


def test_extract_pent_grind_deaths_rejected():
    cand = {"metrics": {"deaths": [{"t_s": 1.0, "killer": "a", "victim": "b"}]}}
    with pytest.raises(ValueError, match="deaths"):
        logic.extract_kill_events(cand)


def test_extract_missing_kills_is_empty():
    assert logic.extract_kill_events({"metrics": {}}) == []
    assert logic.extract_kill_events({}) == []


# ---------------------------------------------------------- overlay_window

def test_window_mode_t0_is_window_start():
    t0, dur = logic.overlay_window(kippo_candidate(), mode="window")
    assert t0 == 726.269
    assert dur == pytest.approx(35.0)


def test_capture_mode_mirrors_render_plan():
    # render_candidate.plan_render (preroll 5, countdown 10):
    # seek = floor(726.269+10-5) = 731 -> t0 = 721, capture = ceil(771.269-731) = 41
    t0, dur = logic.overlay_window(kippo_candidate(), mode="capture",
                                   preroll=5, countdown=10)
    assert t0 == 721.0
    assert dur == 41.0


def test_capture_mode_clamps_seek_at_zero():
    cand = kippo_candidate()
    cand["window"] = {"start_s": 2.0, "end_s": 32.0}
    t0, dur = logic.overlay_window(cand, mode="capture", preroll=5, countdown=10)
    # seek = max(0, floor(2+10-5)) = 7 -> t0 = -3 (3 s of countdown preroll)
    assert t0 == -3.0
    assert dur == math.ceil(32 + 10 - 7)


def test_degenerate_window_raises():
    cand = kippo_candidate()
    cand["window"] = {"start_s": 10.0, "end_s": 10.0}
    with pytest.raises(ValueError, match="degenerate"):
        logic.overlay_window(cand)


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="mode"):
        logic.overlay_window(kippo_candidate(), mode="bogus")


# ---------------------------------------------------------- build_timeline

def test_timeline_offsets_are_t_minus_t0():
    evs = logic.extract_kill_events(kippo_candidate())
    tl = logic.build_timeline(evs, 726.269, 35.0, fps=30)
    assert tl["total_frames"] == 1050
    frames = [e["frame"] for e in tl["events"]]
    # round((t_s - 726.269) * 30)
    assert frames == [152, 449, 561, 772, 789, 921, 971]
    assert tl["dropped"] == []


def test_timeline_increments_within_100ms_of_source():
    evs = logic.extract_kill_events(kippo_candidate())
    tl = logic.build_timeline(evs, 726.269, 35.0, fps=30)
    for ev in tl["events"]:
        assert abs(ev["offset_s"] - (ev["t_s"] - 726.269)) < 0.1


def test_counter_sequence_increments_per_kill():
    evs = logic.extract_kill_events(kippo_candidate())
    tl = logic.build_timeline(evs, 726.269, 35.0)
    assert [e["count"] for e in tl["events"]] == [1, 2, 3, 4, 5, 6, 7]
    assert all(e["type"] == "kill" for e in tl["events"])


def test_teamkills_do_not_increment_and_are_oops():
    evs = logic.extract_kill_events(bogojoker_candidate())
    tl = logic.build_timeline(evs, 781.2, 30.0)
    assert [e["type"] for e in tl["events"]] == ["oops", "oops"]
    assert [e["count"] for e in tl["events"]] == [0, 0]


def test_mixed_kills_and_teamkills_counter():
    evs = [
        {"t_s": 1.0, "teamkill": False},
        {"t_s": 2.0, "teamkill": True},   # oops — count stays 1
        {"t_s": 3.0, "teamkill": False},
    ]
    tl = logic.build_timeline(evs, 0.0, 10.0)
    assert [(e["type"], e["count"]) for e in tl["events"]] == \
        [("kill", 1), ("oops", 1), ("kill", 2)]


def test_simultaneous_kills_queue_min_gap_frames():
    # Milton e1m2 has two kills at exactly 591.5 — both must be visible.
    evs = [
        {"t_s": 591.5, "teamkill": False},
        {"t_s": 591.5, "teamkill": False},
    ]
    tl = logic.build_timeline(evs, 590.0, 10.0, fps=30)
    f1, f2 = (e["frame"] for e in tl["events"])
    assert f2 - f1 == logic.MIN_GAP_FRAMES
    # the queued tick still lands within the 100 ms verification budget
    assert abs(tl["events"][1]["offset_s"] - 1.5) <= 0.1


def test_events_before_t0_dropped():
    evs = [{"t_s": 5.0, "teamkill": False}, {"t_s": 11.0, "teamkill": False}]
    tl = logic.build_timeline(evs, 10.0, 20.0)
    assert len(tl["events"]) == 1
    assert tl["events"][0]["count"] == 1          # count restarts clean
    assert tl["dropped"] == [{"t_s": 5.0, "reason": "before overlay t0"}]


def test_events_past_end_dropped():
    evs = [{"t_s": 1.0, "teamkill": False}, {"t_s": 25.0, "teamkill": False}]
    tl = logic.build_timeline(evs, 0.0, 20.0)
    assert len(tl["events"]) == 1
    assert tl["dropped"] == [{"t_s": 25.0, "reason": "past overlay end"}]


def test_events_sorted_even_if_input_unsorted():
    evs = [{"t_s": 9.0, "teamkill": False}, {"t_s": 3.0, "teamkill": False}]
    tl = logic.build_timeline(evs, 0.0, 20.0)
    assert [e["t_s"] for e in tl["events"]] == [3.0, 9.0]
    assert [e["count"] for e in tl["events"]] == [1, 2]


def test_zero_event_window_is_legal():
    tl = logic.build_timeline([], 0.0, 30.0)
    assert tl["events"] == []
    assert tl["total_frames"] == 900


def test_bad_duration_raises():
    with pytest.raises(ValueError, match="duration"):
        logic.build_timeline([], 0.0, 0.0)


# ----------------------------------------------------- animation keyframes

def test_kill_anim_span_is_pop_frames():
    tl = logic.build_timeline([{"t_s": 1.0, "teamkill": False}], 0.0, 10.0)
    ev = tl["events"][0]
    assert ev["anim_end"] == ev["frame"] + logic.POP_FRAMES


def test_oops_anim_span_is_oops_frames():
    tl = logic.build_timeline([{"t_s": 1.0, "teamkill": True}], 0.0, 10.0)
    ev = tl["events"][0]
    assert ev["anim_end"] == ev["frame"] + logic.OOPS_FRAMES


def test_anim_end_clamped_to_last_frame():
    # kill 0.1 s before the end: pop must not run past total_frames-1
    tl = logic.build_timeline([{"t_s": 9.9, "teamkill": False}], 0.0, 10.0, fps=30)
    ev = tl["events"][0]
    assert ev["anim_end"] == tl["total_frames"] - 1


# ----------------------------------------------------------- dynamic_frames

def test_dynamic_frames_cover_anims_plus_settle_frame():
    tl = logic.build_timeline([{"t_s": 2.0, "teamkill": False}], 0.0, 10.0, fps=30)
    dyn = logic.dynamic_frames(tl)
    ev = tl["events"][0]
    assert dyn[0] == 0                                   # base frame always
    assert set(range(ev["frame"], ev["anim_end"] + 1)) <= set(dyn)
    assert ev["frame"] - 1 not in dyn                    # idle before
    # one settled frame past anim_end IS dynamic (copy source for the idle
    # span — the anim's last frame still renders the active treatment)
    assert ev["anim_end"] + 1 in dyn
    assert ev["anim_end"] + 2 not in dyn                 # idle after
    assert len(dyn) == 1 + logic.POP_FRAMES + 2


def test_dynamic_frames_zero_events():
    tl = logic.build_timeline([], 0.0, 5.0)
    assert logic.dynamic_frames(tl) == [0]


# ------------------------------------------------------------ sfx_schedule

def test_sfx_schedule_matches_visual_ticks():
    evs = logic.extract_kill_events(kippo_candidate())
    tl = logic.build_timeline(evs, 726.269, 35.0)
    sched = logic.sfx_schedule(tl)
    assert [s["offset_s"] for s in sched] == [e["offset_s"] for e in tl["events"]]
    assert all(s["sound"] == "kill" for s in sched)


def test_sfx_pitch_rises_with_combo_and_caps():
    evs = [{"t_s": float(i), "teamkill": False} for i in range(1, 11)]
    tl = logic.build_timeline(evs, 0.0, 20.0)
    sched = logic.sfx_schedule(tl)
    pitches = [s["pitch"] for s in sched]
    assert pitches[0] == 1.0
    assert pitches[1] == pytest.approx(1.0 + logic.SFX_PITCH_STEP)
    assert pitches == sorted(pitches)
    assert max(pitches) <= logic.SFX_PITCH_MAX


def test_sfx_oops_sound_for_teamkills():
    evs = logic.extract_kill_events(bogojoker_candidate())
    tl = logic.build_timeline(evs, 781.2, 30.0)
    sched = logic.sfx_schedule(tl)
    assert [s["sound"] for s in sched] == ["oops", "oops"]
    assert all(s["pitch"] == 1.0 for s in sched)


def test_sfx_filenames_are_placeholder_named():
    assert all(name.startswith("PLACEHOLDER_")
               for name in logic.SFX_FILES.values())


# ----------------------------------------------------------- output naming

def test_output_basename():
    assert logic.output_basename(kippo_candidate()) == \
        "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_killcounter"
    assert logic.output_basename(bogojoker_candidate()) == \
        "2026-06-10_schloss_red-vs-blue_bogojoker-team-killer_killcounter"


# ------------------------------------------------------- font readiness gate
def test_killcounter_html_fails_closed_on_required_font_errors():
    builder = (KC_DIR / "build_overlay.mjs").read_text(encoding="utf-8")

    assert "window.__fontError" in builder
    assert "document.fonts.check" in builder
    assert "window.__ready=true" in builder
    assert ".catch(()=>{})" not in builder


def test_killcounter_renderer_checks_font_error_before_screenshots():
    renderer = (KC_DIR / "render_frames.mjs").read_text(encoding="utf-8")

    assert "window.__ready === true || window.__fontError" in renderer
    assert "window.__fontError || null" in renderer
    assert "font load failed" in renderer
