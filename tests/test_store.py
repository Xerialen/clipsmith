"""Tests for clipsmith.store — round-trip, lifecycle guard, CLI smoke."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from clipsmith import store

REPO_ROOT = Path(__file__).resolve().parents[1]


def sample_candidate():
    """A full candidate with nested metrics incl. per-kill event timestamps."""
    return {
        "id": "cand-0001",
        "category": "QUAD RUNNER",
        "detected_at": "2026-06-10T18:30:00+00:00",
        "demo_sha": "ab" * 32,
        "map": "dm3",
        "matchdate": "2026-06-08",
        "teams": [
            {"name": "tot", "score": 187},
            {"name": "-s-", "score": 142},
        ],
        "pov_player": {"name": "xerial", "userid": 42},
        "window": {"start_s": 312.4, "end_s": 347.9},
        "score": 8.5,
        "metrics": {
            "frags_in_window": 4,
            "quad_pickup_s": 317.4,
            "kills": [
                {"t_s": 319.2, "weapon": "rl", "victim": "bps", "teamkill": False},
                {"t_s": 324.7, "weapon": "rl", "victim": "razor", "teamkill": False},
                {"t_s": 331.0, "weapon": "lg", "victim": "mm", "teamkill": False},
                {"t_s": 340.5, "weapon": "rl", "victim": "gamer", "teamkill": True},
            ],
        },
        "description": "xerial's quad run on dm3: 4 frags incl. 2 RL kills.",
        "hub_url": "https://hub.quakeworld.nu/games?gameId=123&from=312&track=42",
        "status": "candidate",
    }


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "candidates.db")


def test_round_trip(db):
    cand = sample_candidate()
    cand_id = store.add_candidate(cand, db_path=db)
    assert cand_id == cand["id"]
    got = store.get_candidate(cand_id, db_path=db)
    assert got == cand


def test_legal_lifecycle_walk(db):
    cand_id = store.add_candidate(sample_candidate(), db_path=db)
    for status in ("posted", "selected", "rendered", "uploaded",
                   "approved", "published"):
        store.update_status(cand_id, status, db_path=db)
        assert store.get_candidate(cand_id, db_path=db)["status"] == status


def test_revision_loop(db):
    cand_id = store.add_candidate(sample_candidate(), db_path=db)
    for status in ("posted", "selected", "rendered", "uploaded",
                   "revision_requested", "rendered", "uploaded", "approved",
                   "published"):
        store.update_status(cand_id, status, db_path=db)
    assert store.get_candidate(cand_id, db_path=db)["status"] == "published"


def test_selected_can_revert_to_posted_for_discord_reselection(db):
    cand_id = store.add_candidate(sample_candidate(), db_path=db)
    for status in ("posted", "selected", "posted", "selected"):
        store.update_status(cand_id, status, db_path=db)
    assert store.get_candidate(cand_id, db_path=db)["status"] == "selected"


def test_revision_must_rerender_before_reupload(db):
    # Regression: revision_requested -> uploaded would skip the re-render
    cand_id = store.add_candidate(sample_candidate(), db_path=db)
    for status in ("posted", "selected", "rendered", "uploaded",
                   "revision_requested"):
        store.update_status(cand_id, status, db_path=db)
    with pytest.raises(ValueError):
        store.update_status(cand_id, "uploaded", db_path=db)


def test_rejected_outcomes(db):
    c1 = dict(sample_candidate(), id="c1")
    store.add_candidate(c1, db_path=db)
    store.update_status("c1", "rejected", db_path=db)
    c2 = dict(sample_candidate(), id="c2")
    store.add_candidate(c2, db_path=db)
    store.update_status("c2", "posted", db_path=db)
    store.update_status("c2", "rejected", db_path=db)


def test_illegal_transitions(db):
    cand_id = store.add_candidate(sample_candidate(), db_path=db)
    with pytest.raises(ValueError):
        store.update_status(cand_id, "uploaded", db_path=db)  # candidate -> uploaded
    with pytest.raises(ValueError):
        store.update_status(cand_id, "published", db_path=db)
    with pytest.raises(ValueError):
        store.update_status(cand_id, "not-a-status", db_path=db)
    # terminal states stay terminal
    store.update_status(cand_id, "rejected", db_path=db)
    with pytest.raises(ValueError):
        store.update_status(cand_id, "posted", db_path=db)


def test_list_filters(db):
    a = dict(sample_candidate(), id="a", category="QUAD RUNNER")
    b = dict(sample_candidate(), id="b", category="TEAM KILLER")
    store.add_candidate(a, db_path=db)
    store.add_candidate(b, db_path=db)
    store.update_status("b", "posted", db_path=db)
    assert [c["id"] for c in store.list_candidates(db_path=db)] == ["a", "b"]
    assert [c["id"] for c in store.list_candidates(category="TEAM KILLER", db_path=db)] == ["b"]
    assert [c["id"] for c in store.list_candidates(status="posted", db_path=db)] == ["b"]
    assert store.list_candidates(category="QUAD RUNNER", status="posted", db_path=db) == []


def cli(db, *args, stdin=None, expect_ok=True):
    proc = subprocess.run(
        [sys.executable, "-m", "clipsmith.store", "--db", db, *args],
        input=stdin, capture_output=True, text=True, cwd=REPO_ROOT,
    )
    if expect_ok:
        assert proc.returncode == 0, proc.stderr
    return proc


def test_cli_smoke(tmp_path):
    db = str(tmp_path / "cli.db")
    cand = sample_candidate()

    cand_id = cli(db, "add", stdin=json.dumps(cand)).stdout.strip()
    assert cand_id == cand["id"]

    lines = cli(db, "list", "--category", "QUAD RUNNER").stdout.strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == cand

    cli(db, "update-status", cand_id, "posted")

    got = json.loads(cli(db, "get", cand_id).stdout)
    assert got == dict(cand, status="posted")

    # illegal transition exits non-zero
    proc = cli(db, "update-status", cand_id, "published", expect_ok=False)
    assert proc.returncode != 0
    assert "illegal transition" in proc.stderr


def test_cli_add_from_file_and_env_db(tmp_path):
    db = str(tmp_path / "env.db")
    cand_file = tmp_path / "cand.json"
    cand = dict(sample_candidate(), id="from-file")
    cand_file.write_text(json.dumps(cand), encoding="utf-8")
    import os
    env = dict(os.environ, CLIPSMITH_DB=db)
    proc = subprocess.run(
        [sys.executable, "-m", "clipsmith.store", "add", str(cand_file)],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "from-file"
    assert store.get_candidate("from-file", db_path=db) == cand
