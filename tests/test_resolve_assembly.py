"""Tests for scripts/assembly/resolve_assembly/logic.py — pure logic only.

No Resolve, no ffmpeg, no MCP here (assemble.py drives those and is
validated live against the real kippo candidate). These pin artifact path
resolution by naming convention, the clip in-point alignment math, the
6-element timeline plan, the 150-frame still butt-join, the `_mcp_` prefix
handling, the beatforge auto-pick, and the locked delivery settings.
"""
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RA_DIR = REPO_ROOT / "scripts" / "assembly" / "resolve_assembly"

spec = importlib.util.spec_from_file_location("ra_logic", RA_DIR / "logic.py")
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)


# The real kippo QUAD RUNNER candidate (store id 43f8ffef…), abridged to the
# fields the assembly logic consumes.
def kippo_candidate():
    return {
        "id": "43f8ffef8bb4463a96c85afb7767ffd4",
        "category": "QUAD RUNNER",
        "map": "dm3",
        "matchdate": "2026-06-08",
        "teams": [{"name": "-fu-", "score": 210}, {"name": "mix", "score": 94}],
        "pov_player": {"name": "kippo (FU)", "userid": 58},
        "window": {"start_s": 726.269, "end_s": 761.269},
    }


# ----------------------------------------------------------------- naming

def test_asset_stem_matches_established_naming():
    assert (
        logic.asset_stem(kippo_candidate())
        == "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner"
    )


def test_raw_capture_names_priority_order():
    names = logic.raw_capture_names(kippo_candidate())
    assert names == (
        "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_43f8ffef_1pov_raw.mov",
        "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_1pov_raw.mov",
        "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_1pov_raw.mp4",
        "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_1pov.mov",
        "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_1pov.mp4",
    )


def test_title_basename_iso_week():
    # 2026-06-08 is ISO week 24; matches the existing generated asset
    # week24_quad-runner_kippo-fu.mp4
    assert logic.title_basename(kippo_candidate()) == "week24_quad-runner_kippo-fu"


def test_killcounter_basename_matches_existing_asset():
    assert (
        logic.killcounter_basename(kippo_candidate())
        == "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_killcounter"
    )


def test_output_name_assembled_suffix():
    assert (
        logic.output_name(kippo_candidate())
        == "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_assembled.mp4"
    )


def test_resolve_artifacts_prefers_locked_mov_then_falls_back(tmp_path):
    raw = tmp_path / "raw"
    assets = tmp_path / "assets"
    out = tmp_path / "out"
    for d in (raw, assets, out):
        d.mkdir()
    cand = kippo_candidate()
    # Only the PoC-era name exists -> falls back to it.
    poc = raw / "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_1pov.mp4"
    poc.write_bytes(b"x")
    art = logic.resolve_artifacts(cand, raw, assets, out)
    assert art["raw"] == poc
    # The new id-bearing locked .mov name appears -> takes priority.
    locked = raw / (
        "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_"
        "43f8ffef_1pov_raw.mov"
    )
    locked.write_bytes(b"x")
    art = logic.resolve_artifacts(cand, raw, assets, out)
    assert art["raw"] == locked
    # Conventional generated/derived paths regardless of existence.
    assert art["title_card"] == assets / "week24_quad-runner_kippo-fu.mp4"
    assert art["title_audio"] == assets / "week24_quad-runner_kippo-fu.wav"
    assert art["killcounter"].name.endswith("_killcounter.mov")
    assert art["killcounter_sfx"].name.endswith("_killcounter_sfx.wav")
    assert art["frame_overlay"].name.endswith("_frame.png")
    # V2 places the clip-length qtrle render of the frame, never the still
    # (stills import as 1-frame FPS-24 clips and place non-deterministically
    # at fps != 30 — measured 2026-06-11 on the 60 fps BLooD_DoG run).
    assert art["frame_overlay_video"].name.endswith("_frame.mov")
    assert art["frame_overlay_video"].parent == assets
    assert art["output"] == out / (
        "2026-06-08_dm3_fu-vs-mix_kippo-fu-quad-runner_assembled.mp4"
    )


def test_resolve_artifacts_missing_raw_is_none(tmp_path):
    art = logic.resolve_artifacts(kippo_candidate(), tmp_path, tmp_path, tmp_path)
    assert art["raw"] is None


# ------------------------------------------------------------- lower-third

def test_parse_player_clan_variants():
    assert logic.parse_player("kippo (FU)") == ("KIPPO", "FU")
    assert logic.parse_player("rusti [FU]") == ("RUSTI", "FU")
    assert logic.parse_player("Milton") == ("MILTON", None)


def test_lowerthird_fields():
    assert logic.lowerthird_fields(kippo_candidate()) == {
        "player": "KIPPO",
        "clan": "FU",
        "map": "DM3",
    }


# --------------------------------------------------------- clip alignment

def test_clip_in_offset_mirrors_render_plan():
    # kippo: seek = floor(726.269 + 10 - 5) = 731; capture t0 = 721
    # -> window starts 5.269 s into the capture.
    assert logic.clip_in_offset_s(kippo_candidate()) == pytest.approx(5.269)


def test_clip_in_offset_clamped_seek():
    # Early window, default preroll/countdown: seek = floor(2+10-5) = 7,
    # capture t0 = -3 -> window lands 5 s in (floor() already absorbed
    # the fraction).
    cand = kippo_candidate()
    cand["window"] = {"start_s": 2.0, "end_s": 30.0}
    assert logic.clip_in_offset_s(cand) == pytest.approx(5.0)
    # Seek clamps at 0 when the requested preroll reaches past the demo
    # start (preroll > start + countdown): capture t0 = -countdown, so the
    # window lands at start + countdown.
    assert logic.clip_in_offset_s(cand, preroll=15.0) == pytest.approx(12.0)


def test_window_duration_and_degenerate_window():
    assert logic.window_duration_s(kippo_candidate()) == pytest.approx(35.0)
    cand = kippo_candidate()
    cand["window"] = {"start_s": 10.0, "end_s": 10.0}
    with pytest.raises(ValueError):
        logic.window_duration_s(cand)
    with pytest.raises(ValueError):
        logic.clip_in_offset_s(cand)


# ------------------------------------------------------------ timeline math

def test_plan_timeline_six_element_structure():
    plan = logic.plan_timeline(3.5, 3.5, 35.0, 3.5, fps=30)
    segs = plan["segments"]
    assert plan["order"] == ["intro", "title", "clip", "outro"]
    assert segs["intro"] == {"name": "intro", "offset_frames": 0, "frames": 105}
    assert segs["title"] == {"name": "title", "offset_frames": 105, "frames": 105}
    assert segs["clip"] == {"name": "clip", "offset_frames": 210, "frames": 1050}
    assert segs["outro"] == {"name": "outro", "offset_frames": 1260, "frames": 105}
    assert plan["total_frames"] == 1365
    assert plan["total_s"] == pytest.approx(45.5)


def test_plan_timeline_frame_quantization_keeps_segments_contiguous():
    # Non-frame-aligned durations: each segment rounds independently but
    # offsets stay contiguous (no gaps/overlaps on V1).
    plan = logic.plan_timeline(3.33, 3.5, 35.02, 3.33, fps=30)
    cursor = 0
    for name in plan["order"]:
        seg = plan["segments"][name]
        assert seg["offset_frames"] == cursor
        cursor += seg["frames"]
    assert plan["total_frames"] == cursor


def test_plan_timeline_rejects_degenerate():
    with pytest.raises(ValueError):
        logic.plan_timeline(0.0, 3.5, 35.0, 3.5, fps=30)
    with pytest.raises(ValueError):
        logic.plan_timeline(3.5, 3.5, 35.0, 3.5, fps=0)


def test_still_chunks_butt_join_cap():
    # 35 s @30 = 1050 frames -> 7 full 150-frame stills.
    assert logic.still_chunks(1050) == [150] * 7
    assert logic.still_chunks(310) == [150, 150, 10]
    assert logic.still_chunks(10) == [10]
    assert logic.still_chunks(150) == [150]
    assert sum(logic.still_chunks(1051)) == 1051
    with pytest.raises(ValueError):
        logic.still_chunks(0)


# ----------------------------------------------------------------- music

def test_pick_music_filters_and_is_deterministic():
    files = [
        "beat_1772834240.wav",
        "beat_1773005444.wav",
        "beat_ref_1773034540.wav",  # reference track — excluded
        "parody_1772866105.wav",    # parody — excluded
        "notes.txt",
    ]
    cand_id = kippo_candidate()["id"]
    pick = logic.pick_music(files, cand_id)
    assert pick in {"beat_1772834240.wav", "beat_1773005444.wav"}
    assert logic.pick_music(files, cand_id) == pick  # stable
    # listing order must not matter
    assert logic.pick_music(list(reversed(files)), cand_id) == pick


def test_pick_music_none_when_no_beats():
    assert logic.pick_music(["parody_1.wav", "x.txt"], "ab" * 16) is None


# ----------------------------------------------------------- Resolve naming

def test_project_name_uses_mcp_prefix_and_id8():
    assert (
        logic.project_name(kippo_candidate()["id"])
        == "_mcp_clipsmith_43f8ffef"
    )


def test_mcp_prefix_helpers():
    assert logic.ensure_mcp_prefix("clipsmith_x") == "_mcp_clipsmith_x"
    assert logic.ensure_mcp_prefix("_mcp_clipsmith_x") == "_mcp_clipsmith_x"
    assert logic.strip_mcp_prefix("_mcp_clipsmith_x") == "clipsmith_x"
    assert logic.strip_mcp_prefix("clipsmith_x") == "clipsmith_x"
    assert logic.project_lookup_names("_mcp_clipsmith_x") == (
        "_mcp_clipsmith_x",
        "clipsmith_x",
    )
    assert logic.project_lookup_names("clipsmith_x") == (
        "clipsmith_x",
        "_mcp_clipsmith_x",
    )


# -------------------------------------------------------------- delivery

def test_delivery_settings_locked_30fps():
    d = logic.delivery_settings(30)
    assert d["format"] == "mp4"
    assert d["video_codec"] == "H264"
    assert (d["width"], d["height"]) == (1920, 1080)
    assert d["bitrate_kbps"] == 8000      # 1080p below 50 fps
    assert d["keyframe_interval"] == 15   # half the frame rate
    assert d["closed_gop"] is True
    assert d["b_frames"] == 2
    assert d["h264_profile"] == "High"
    assert d["audio_codec"] == "aac"
    assert d["audio_bitrate_kbps"] == 384
    assert d["audio_sample_rate"] == 48000


def test_delivery_settings_locked_60fps():
    d = logic.delivery_settings(60)
    assert d["bitrate_kbps"] == 12000
    assert d["keyframe_interval"] == 30
