"""Tests for scripts/produce_clip.py — pure logic only (issue #26).

No WSL, no Resolve, no ffmpeg/ffprobe, no store DB here: stage planning,
ISO-week derivation, artifact-name/argv wiring, render skip logic, and the
fail-fast pipeline runner with a fake runner injected.
"""
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "produce_clip", REPO_ROOT / "scripts" / "produce_clip.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


# The real BLooD_DoG QUAD RUNNER candidate (store id 397cb3b5…), abridged to
# the fields the driver consumes.
def blood_dog_candidate(**over):
    cand = {
        "id": "397cb3b5437049c4b26a4bc10f0a7051",
        "category": "QUAD RUNNER",
        "map": "dm2",
        "matchdate": "2026-06-10",
        "teams": [{"name": "blue", "score": 206}, {"name": "red", "score": 187}],
        "pov_player": {"name": "BLooD_DoG(D_P)", "userid": 76},
        "window": {"start_s": 121.598, "end_s": 156.598},
        "status": "selected",
    }
    cand.update(over)
    return cand


# ------------------------------------------------------------------ fps parse

def test_parse_fps_locked_capture():
    assert pc.parse_fps("60/1") == 60


def test_parse_fps_legacy_30():
    assert pc.parse_fps("30/1") == 30


def test_parse_fps_rejects_ntsc():
    with pytest.raises(ValueError, match="non-integer"):
        pc.parse_fps("30000/1001")


# ------------------------------------------------------------- week derivation

def test_week_derivation_matches_title_card_naming():
    # 2026-06-08 (the kippo card, week24_…) and 2026-06-10 are both ISO wk 24.
    assert pc.derive_week("2026-06-08") == 24
    assert pc.derive_week("2026-06-10") == 24


def test_week_derivation_year_boundary():
    assert pc.derive_week("2026-01-01") == 1   # 2026-01-01 is a Thursday
    assert pc.derive_week("2027-01-01") == 53  # belongs to ISO 2026-W53


# ----------------------------------------------------------------- raw lookup

def test_find_raw_prefers_locked_mov(tmp_path):
    cand = blood_dog_candidate()
    unique = (
        "2026-06-10_dm2_blue-vs-red_blood-dog-d-p-quad-runner_"
        "397cb3b5_1pov_raw"
    )
    legacy = "2026-06-10_dm2_blue-vs-red_blood-dog-d-p-quad-runner_1pov_raw"
    (tmp_path / f"{legacy}.mov").touch()
    (tmp_path / f"{unique}.mov").touch()
    assert pc.find_raw(cand, tmp_path).name == f"{unique}.mov"


def test_find_raw_none_when_absent(tmp_path):
    assert pc.find_raw(blood_dog_candidate(), tmp_path) is None


# ---------------------------------------------------------------- skip logic

def test_render_decision_selected_no_raw_runs():
    assert pc.render_decision("selected", raw_exists=False) == "run"


def test_render_decision_selected_with_raw_skips_and_flips():
    assert pc.render_decision("selected", raw_exists=True) == "skip-flip"


def test_render_decision_rendered_with_raw_resumes():
    assert pc.render_decision("rendered", raw_exists=True) == "skip"


def test_render_decision_rendered_without_raw_errors():
    with pytest.raises(ValueError, match="raw capture is missing"):
        pc.render_decision("rendered", raw_exists=False)


@pytest.mark.parametrize("status", ["candidate", "posted", "rejected",
                                    "uploaded", "published"])
def test_render_decision_rejects_wrong_entry_status(status):
    with pytest.raises(ValueError, match="need 'selected'"):
        pc.render_decision(status, raw_exists=False)
    with pytest.raises(ValueError, match="need 'selected'"):
        pc.render_decision(status, raw_exists=True)


# ---------------------------------------------------------- intro fps conform

def test_intro_conform_decision_missing_intro_defers_to_assemble():
    assert pc.intro_conform_decision(False, None, 60) == "default"


def test_intro_conform_decision_matching_fps_passes_through():
    assert pc.intro_conform_decision(True, 60, 60) == "use"


def test_intro_conform_decision_mismatch_conforms():
    # The measured 2026-06-11 failure: 30 fps placeholder on a 60 fps
    # timeline came out 420 frames/segment instead of 210 in Resolve.
    assert pc.intro_conform_decision(True, 30, 60) == "conform"


def test_conform_intro_command_doubles_frames_copies_audio():
    argv = pc.conform_intro_command(r"a\intro.mp4", r"g\intro_60fps.mp4", 60)
    assert argv[argv.index("-vf") + 1] == "fps=60"
    assert argv[argv.index("-c:a") + 1] == "copy"  # no second lossy generation
    assert argv[-1] == r"g\intro_60fps.mp4"
    assert "-y" in argv  # always re-conformed, never a stale cache


def test_stage_commands_intro_passthrough():
    cand = blood_dog_candidate()
    cmds = pc.stage_commands(cand, 60, "r", "a", "o",
                             intro=r"g\intro_60fps.mp4")
    asm = cmds["assemble"]
    assert asm[asm.index("--intro") + 1] == r"g\intro_60fps.mp4"
    assert "--intro" not in cmds["title_card"]
    # default: no --intro flag at all (assemble.py uses its own default)
    assert "--intro" not in pc.stage_commands(cand, 60, "r", "a", "o")["assemble"]


# ------------------------------------------------------------- command wiring

def test_render_command_isolation_and_db():
    argv = pc.render_command("cand123", db="state/x.db", raw_dir=r"R:\raw")
    assert argv[:3] == [pc.sys.executable, "-m", pc.RENDER_MODULE]
    assert argv[argv.index("--display") + 1] == "97"
    assert argv[argv.index("--staging-root") + 1] == "/root/render-97"
    assert argv[argv.index("--final-dir") + 1] == r"R:\raw"
    assert argv[argv.index("--db") + 1] == "state/x.db"
    assert "cand123" in argv


def test_stage_commands_fps_and_artifact_wiring():
    cand = blood_dog_candidate()
    cmds = pc.stage_commands(cand, 60, r"R:\raw", r"A:\gen", r"O:\branded",
                             python="py")
    title = cmds["title_card"]
    assert title[title.index("--category") + 1] == "QUAD RUNNER"
    assert title[title.index("--player") + 1] == "BLooD_DoG(D_P)"
    assert title[title.index("--week") + 1] == "24"
    assert title[title.index("--fps") + 1] == "60"
    assert title[title.index("--out-dir") + 1] == r"A:\gen"

    kc = cmds["killcounter"]
    assert kc[2] == cand["id"]
    assert kc[kc.index("--fps") + 1] == "60"
    assert kc[kc.index("--outdir") + 1] == r"A:\gen"
    assert "--db" not in kc  # default db -> no flag passed down

    asm = cmds["assemble"]
    assert asm[2] == cand["id"]
    assert asm[asm.index("--raw-dir") + 1] == r"R:\raw"
    assert asm[asm.index("--assets-dir") + 1] == r"A:\gen"
    assert asm[asm.index("--out-dir") + 1] == r"O:\branded"


def test_stage_commands_generated_names_agree_with_assembly_lookup():
    """The title card the CLI will write == the name assemble.py looks for."""
    cand = blood_dog_candidate()
    cmds = pc.stage_commands(cand, 60, "r", "a", "o")
    title = cmds["title_card"]
    week = int(title[title.index("--week") + 1])
    # ra_logic.title_basename is what resolve_artifacts uses to find the card.
    assert pc.ra_logic.title_basename(cand) == \
        f"week{week:02d}_quad-runner_blood-dog-d-p"
    assert pc.ra_logic.killcounter_basename(cand) == \
        "2026-06-10_dm2_blue-vs-red_blood-dog-d-p-quad-runner_killcounter"


def test_stage_commands_db_passthrough():
    cmds = pc.stage_commands(blood_dog_candidate(), 60, "r", "a", "o",
                             db="state/x.db")
    for name in ("killcounter", "assemble"):
        argv = cmds[name]
        assert argv[argv.index("--db") + 1] == "state/x.db"
    # the title-card CLI has no --db (it never touches the store)
    assert "--db" not in cmds["title_card"]


# ------------------------------------------------------------ pipeline runner

def fake_clock_seq(*ticks):
    it = iter(ticks)
    return lambda: next(it)


def test_run_pipeline_order_and_timing():
    calls = []

    def runner(name, argv):
        calls.append(name)
        return 0

    timings = pc.run_pipeline(
        [("title_card", ["t"]), ("killcounter", ["k"]), ("assemble", ["a"])],
        runner, clock=fake_clock_seq(0, 5, 5, 11, 11, 14))
    assert calls == ["title_card", "killcounter", "assemble"]
    assert timings == {"title_card": 5, "killcounter": 6, "assemble": 3}


def test_run_pipeline_fail_fast_carries_stage_name():
    calls = []

    def runner(name, argv):
        calls.append(name)
        return 4 if name == "killcounter" else 0

    with pytest.raises(pc.StageError) as ei:
        pc.run_pipeline(
            [("title_card", ["t"]), ("killcounter", ["k"]),
             ("assemble", ["a"])],
            runner, clock=fake_clock_seq(0, 1, 1, 2))
    assert calls == ["title_card", "killcounter"]  # assemble never ran
    assert ei.value.stage == "killcounter"
    assert ei.value.code == 4
    assert "killcounter" in str(ei.value)
    assert ei.value.timings == {"title_card": 1, "killcounter": 1}


def test_format_timings_marks_skipped_render():
    out = pc.format_timings({"render": 0.0, "title_card": 65.0,
                             "killcounter": 30.0, "assemble": 120.5})
    assert "render" in out and "skipped" in out
    assert "1:05.0" in out      # title_card
    assert "3:35.5" in out      # total 215.5 s
