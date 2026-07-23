"""Tests for clipsmith.render — pure logic only.

Everything WSL/engine/ffmpeg-shaped is out of scope here (it runs in the
clipsmith-render distro); these tests pin the cfg generation, the
countdown/preroll seek math, the output naming scheme, and the worker
script's load-bearing invariants as text.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from clipsmith import store
from clipsmith import render as rc

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_DIR = REPO_ROOT / "scripts" / "render"


def sample_candidate(**over):
    cand = {
        "id": "cand-0001",
        "category": "QUAD RUNNER",
        "demo_sha": "ab" * 32,
        "map": "e1m2",
        "matchdate": "2026-06-09",
        "teams": [{"name": "red", "score": 227}, {"name": "blue", "score": 180}],
        "pov_player": {"name": "rusti (FU)", "userid": 47},
        "window": {"start_s": 868.919, "end_s": 903.919},
        "status": "selected",
    }
    cand.update(over)
    return cand


def stored_candidate(**over):
    cand = sample_candidate(
        detected_at="2026-06-10T18:30:00+00:00",
        score=8.5,
        metrics={},
        description="rusti quad run",
        hub_url="https://hub.quakeworld.nu/games?gameId=123&from=868&track=47",
    )
    cand.update(over)
    return cand


# ----------------------------------------------------------- plan_render

def test_plan_seek_is_start_plus_countdown_minus_preroll():
    plan = rc.plan_render(sample_candidate(), preroll=5, countdown=10, fps=30)
    # floor(868.919 + 10 - 5) = 873; ceil(903.919 + 10 - 873) = 41
    assert plan["seek_s"] == 873
    assert plan["capture_s"] == 41
    assert plan["expected_frames"] == 41 * 30
    assert plan["userid"] == 47


def test_plan_covers_full_window():
    cand = sample_candidate(window={"start_s": 298.116, "end_s": 333.116})
    plan = rc.plan_render(cand)
    assert plan["seek_s"] <= 298.116 + 10 - 5
    assert plan["seek_s"] + plan["capture_s"] >= 333.116 + 10


def test_plan_clamps_seek_at_zero_for_match_start_windows():
    cand = sample_candidate(window={"start_s": 0.0, "end_s": 34.921})
    plan = rc.plan_render(cand, preroll=15, countdown=10)
    assert plan["seek_s"] == 0
    assert plan["capture_s"] >= 45  # still covers end_s + countdown


def test_plan_rejects_degenerate_window():
    cand = sample_candidate(window={"start_s": 10.0, "end_s": 10.0})
    with pytest.raises(ValueError):
        rc.plan_render(cand)


# ----------------------------------------------- locked picture settings

def test_locked_capture_defaults_match_picture_settings_doc():
    """issue #22: capture locked at 1920x1080@60 (docs/picture-settings.md)."""
    assert (rc.DEFAULT_WIDTH, rc.DEFAULT_HEIGHT, rc.DEFAULT_FPS) == (1920, 1080, 60)


def test_hud_space_lock_only_above_720p():
    # at 720p the console space equals the framebuffer — no lock emitted
    assert rc.hud_space_lines(1280, 720) == []
    # at 1080p the parity HUD keeps its tuned 720p coordinate space
    assert rc.hud_space_lines(1920, 1080) == \
        ["vid_conwidth 1280", "vid_conheight 720"]


# ------------------------------------------------------------- cfg pair

def test_arm_cfg_trigger_exec_pattern():
    cfg = rc.build_arm_cfg("clipsmith_job.mvd", fps=30)
    assert 're_trigger clipsmith_arm "matchdate:"' in cfg
    assert "exec clipsmith_capgo.cfg" in cfg          # cbuf_safe escape hatch
    assert "playdemo demos/clipsmith_job.mvd" in cfg
    assert "demo_capture_fps 30" in cfg
    assert "cfg_save_onquit 0" in cfg
    assert "re_trigger_disable clipsmith_arm" in cfg  # one-shot trigger


def test_arm_cfg_execs_parity_hud_with_console_space_lock_at_1080p():
    cfg = rc.build_arm_cfg("clipsmith_job.mvd", fps=60, width=1920, height=1080)
    assert "exec parity_hud.cfg" in cfg
    assert "vid_conwidth 1280" in cfg
    assert "vid_conheight 720" in cfg
    # the HUD layer must be exec'd BEFORE the console-space lock and BEFORE
    # the demo starts playing
    assert cfg.index("exec parity_hud.cfg") < cfg.index("vid_conwidth 1280")
    assert cfg.index("vid_conwidth 1280") < cfg.index("playdemo")
    assert "demo_capture_fps 60" in cfg


def test_arm_cfg_no_console_space_lock_at_720p():
    cfg = rc.build_arm_cfg("clipsmith_job.mvd", fps=30, width=1280, height=720)
    assert "exec parity_hud.cfg" in cfg   # parity HUD always applies
    assert "vid_conwidth" not in cfg
    assert "vid_conheight" not in cfg


def test_capgo_cfg_order_autotrack_jump_track_capture():
    cfg = rc.build_capgo_cfg(seek_s=873, userid=47, capture_s=41)
    for line in ("mvd_autotrack 0", "demo_autotrack 0", "cl_hightrack 0",
                 "demo_jump 873", "track 47", "toggleconsole", "con_notify 0",
                 "demo_capture start 41"):
        assert line in cfg, line
    # Order is load-bearing: autotrack cleared BEFORE the jump, track AFTER
    # the seek lands, capture last.
    assert cfg.index("mvd_autotrack 0") < cfg.index("demo_jump 873")
    assert cfg.index("demo_jump 873") < cfg.index("track 47")
    assert cfg.index("track 47") < cfg.index("demo_capture start 41")
    # The waits between jump and track let the seek land.
    between = cfg[cfg.index("demo_jump 873"):cfg.index("track 47")]
    assert "w25" in between


def test_capgo_cfg_has_no_hardcoded_leftovers():
    cfg = rc.build_capgo_cfg(seek_s=303, userid=93, capture_s=41)
    assert "demo_jump 303" in cfg
    assert "track 93" in cfg
    assert "58" not in cfg   # the old kippo uid
    assert "718" not in cfg  # the old kippo seek


# --------------------------------------------------------------- naming

def test_output_basename_established_scheme():
    assert rc.output_basename(sample_candidate()) == \
        "2026-06-09_e1m2_red-vs-blue_rusti-fu-quad-runner_cand-000_1pov_raw"


def test_output_basename_slugifies_hostile_names():
    cand = sample_candidate(
        category="PENT RUNNER",
        map="schloss",
        matchdate="2026-06-10",
        teams=[{"name": "-fu-", "score": 210}, {"name": "mix", "score": 94}],
        pov_player={"name": "rgh   (FU)", "userid": 50},
    )
    assert rc.output_basename(cand) == \
        "2026-06-10_schloss_fu-vs-mix_rgh-fu-pent-runner_cand-000_1pov_raw"


def test_output_basename_distinct_for_same_match_candidates():
    a = sample_candidate(id="aaaa1111" + "0" * 24)
    b = sample_candidate(id="bbbb2222" + "0" * 24)
    assert rc.output_basename(a) != rc.output_basename(b)
    assert "aaaa1111" in rc.output_basename(a)
    assert "bbbb2222" in rc.output_basename(b)


def test_final_output_path_uses_canonical_mov_name(tmp_path):
    assert rc.final_output_path(sample_candidate(), tmp_path) == \
        tmp_path / "2026-06-09_e1m2_red-vs-blue_rusti-fu-quad-runner_cand-000_1pov_raw.mov"


def test_usable_raw_exists_requires_nonempty_regular_file(tmp_path):
    missing = tmp_path / "missing.mov"
    empty = tmp_path / "empty.mov"
    raw = tmp_path / "raw.mov"
    empty.touch()
    raw.write_bytes(b"raw")

    assert rc.usable_raw_exists(missing) is False
    assert rc.usable_raw_exists(empty) is False
    assert rc.usable_raw_exists(tmp_path) is False
    assert rc.usable_raw_exists(raw) is True


def test_render_decision_selected_no_raw_runs():
    assert rc.render_decision("selected", final_exists=False) == "run"


def test_render_decision_selected_with_raw_skips_and_flips():
    assert rc.render_decision("selected", final_exists=True) == "skip-flip"


def test_render_decision_rendered_with_raw_is_noop():
    assert rc.render_decision("rendered", final_exists=True) == "skip"


def test_render_decision_rendered_without_raw_errors():
    with pytest.raises(ValueError, match="canonical raw capture is missing"):
        rc.render_decision("rendered", final_exists=False)


@pytest.mark.parametrize("status", ["candidate", "posted", "uploaded",
                                    "published", "rejected"])
def test_render_decision_rejects_wrong_entry_status(status):
    with pytest.raises(ValueError, match="need 'selected'"):
        rc.render_decision(status, final_exists=False)


def test_render_decision_force_status_always_runs():
    assert rc.render_decision("published", final_exists=True,
                              force_status=True) == "run"


def test_slugify():
    assert rc.slugify("BLooD_DoG(D_P)") == "blood-dog-d-p"
    assert rc.slugify("kippo (FU)") == "kippo-fu"
    assert rc.slugify("Spectator") == "spectator"


# ------------------------------------------------------------- plumbing

def test_win_to_wsl():
    assert rc.win_to_wsl(r"C:\Users\example\wsl\out") == "/mnt/c/Users/example/wsl/out"
    assert rc.win_to_wsl("G:\\My Drive\\x") == "/mnt/g/My Drive/x"
    with pytest.raises(ValueError):
        rc.win_to_wsl("relative/path")


def test_build_job_env_quotes_and_rejects_unsafe():
    env = rc.build_job_env({"A": 1, "B": "two words"})
    assert "A='1'\n" in env
    assert "B='two words'\n" in env
    with pytest.raises(ValueError):
        rc.build_job_env({"BAD": "single ' quote"})


# --------------------------------------------------------------- CLI reruns

def render_cli(db, cand_id, *args):
    return subprocess.run(
        [sys.executable, "-m", "clipsmith.render", cand_id,
         "--db", str(db), *args],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


def last_json_line(stdout):
    return json.loads(stdout.strip().splitlines()[-1])


def test_render_cli_dry_run_is_side_effect_free(tmp_path):
    db = tmp_path / "candidates.db"
    cand = stored_candidate()
    store.add_candidate(cand, db_path=db)
    stage_dir = tmp_path / "stage"
    final_dir = tmp_path / "raw"

    proc = render_cli(
        db, cand["id"],
        "--stage-dir", str(stage_dir),
        "--final-dir", str(final_dir),
        "--demos-dir", str(tmp_path / "missing-demos"),
        "--dry-run",
    )

    assert proc.returncode == 0, proc.stderr
    summary = last_json_line(proc.stdout)
    assert summary["dry_run"] is True
    assert summary["decision"] == "run"
    assert summary["would_update_status"] == "rendered"
    assert store.get_candidate(cand["id"], db_path=db)["status"] == "selected"
    assert not stage_dir.exists()
    assert not final_dir.exists()


def test_render_cli_existing_raw_skips_worker_and_flips_status(tmp_path):
    db = tmp_path / "candidates.db"
    cand = stored_candidate()
    store.add_candidate(cand, db_path=db)
    stage_dir = tmp_path / "stage"
    final_dir = tmp_path / "raw"
    final_dir.mkdir()
    final = rc.final_output_path(cand, final_dir)
    final.write_bytes(b"raw")

    proc = render_cli(
        db, cand["id"],
        "--stage-dir", str(stage_dir),
        "--final-dir", str(final_dir),
        "--demos-dir", str(tmp_path / "missing-demos"),
    )

    assert proc.returncode == 0, proc.stderr
    summary = last_json_line(proc.stdout)
    assert summary["skipped"] is True
    assert summary["status"] == "rendered"
    assert summary["bytes"] == 3
    assert store.get_candidate(cand["id"], db_path=db)["status"] == "rendered"
    assert not stage_dir.exists()


def test_render_cli_zero_byte_raw_does_not_skip_or_flip_status(tmp_path):
    db = tmp_path / "candidates.db"
    cand = stored_candidate()
    store.add_candidate(cand, db_path=db)
    stage_dir = tmp_path / "stage"
    final_dir = tmp_path / "raw"
    final_dir.mkdir()
    final = rc.final_output_path(cand, final_dir)
    final.touch()

    proc = render_cli(
        db, cand["id"],
        "--stage-dir", str(stage_dir),
        "--final-dir", str(final_dir),
        "--demos-dir", str(tmp_path / "missing-demos"),
        "--dry-run",
    )

    assert proc.returncode == 0, proc.stderr
    summary = last_json_line(proc.stdout)
    assert summary["decision"] == "run"
    assert summary["would_update_status"] == "rendered"
    assert store.get_candidate(cand["id"], db_path=db)["status"] == "selected"
    assert not stage_dir.exists()


def test_render_cli_dry_run_force_status_revision_requested_reports_rendered(tmp_path):
    db = tmp_path / "candidates.db"
    cand = stored_candidate(status="revision_requested")
    store.add_candidate(cand, db_path=db)
    stage_dir = tmp_path / "stage"
    final_dir = tmp_path / "raw"

    proc = render_cli(
        db, cand["id"],
        "--stage-dir", str(stage_dir),
        "--final-dir", str(final_dir),
        "--demos-dir", str(tmp_path / "missing-demos"),
        "--dry-run",
        "--force-status",
    )

    assert proc.returncode == 0, proc.stderr
    summary = last_json_line(proc.stdout)
    assert summary["decision"] == "run"
    assert summary["would_update_status"] == "rendered"
    assert store.get_candidate(cand["id"], db_path=db)["status"] == "revision_requested"
    assert not stage_dir.exists()


# ------------------------------------------- worker invariants (as text)

def test_worker_script_encodes_render_gotchas():
    text = (RENDER_DIR / "render_worker.sh").read_text()
    assert "+set vid_renderer 0" in text       # classic renderer mandatory
    assert "Xvfb" in text                      # headless display
    assert "signalstats" in text               # early frame validation
    assert "blackdetect=d=0.5:pix_th=0.05" in text
    assert "cp -al" in text                    # ext4 hardlink-staged basedir
    assert "pkill -f ezquake" not in text      # never kill the parallel render
    assert "gunzip" in text
    assert "\r" not in text                    # LF-only: bash in the distro


def test_worker_script_atomic_demo_staging_and_short_capture_probe():
    text = (RENDER_DIR / "render_worker.sh").read_text()
    assert "$DEMO_NAME.part" in text
    assert "mv \"$DEMO_TMP\" \"$BASEDIR/qw/demos/$DEMO_NAME\"" in text
    assert "PROBE_FRAME" in text
    assert "EXPECTED_FRAMES - 2" in text


def test_worker_script_encodes_locked_intermediate_settings():
    """issue #22: the raw clip is a Resolve ingest master (picture-settings.md).

    H.264 CRF 14 preset slow, 1 s GOP, PCM audio in a .mov container — and no
    leftovers of the PoC settings (CRF 20 / AAC / .mp4 output).
    """
    text = (RENDER_DIR / "render_worker.sh").read_text()
    assert "-crf 14" in text
    assert "-preset slow" in text
    assert '-g "$FPS"' in text                 # 1 s keyframes for scrubbing
    assert "pcm_s16le" in text                 # lossless audio until Resolve
    assert '"$JOB/$OUT_NAME.mov"' in text
    assert "parity_hud.cfg" in text            # HUD layer ships with the job
    assert "-crf 20" not in text
    assert "-c:a aac" not in text
    assert "$OUT_NAME.mp4" not in text


def test_worker_and_cfgs_have_no_hardcoded_candidate_values():
    """Definition of done: no hardcoded sha/window/uid in the orchestrated path."""
    for path in (RENDER_DIR / "render_worker.sh", REPO_ROOT / "clipsmith" / "render.py"):
        text = path.read_text()
        for needle in ("eb7f44", "c92857", "demo_jump 718", "demo_jump 288",
                       "track 58", "clipsmith_hx018"):
            assert needle not in text, f"{needle} in {path}"
