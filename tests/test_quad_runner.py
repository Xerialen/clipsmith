"""Tests for the QUAD RUNNER detector — window extension rule (pure,
offline), scoring/exclusion rules, a mocked end-to-end detect() run, and
idempotent candidate emission (issue #32, shared common.emit_candidate).
No network: mvd-api responses are canned via a fake client."""
import json
import logging

import pytest

from clipsmith import store
from clipsmith.detectors import common
from clipsmith.detectors import quad_runner


# ---------------------------------------------------------------- window math

class TestComputeWindow:
    def test_base_window_when_no_kills_past_30s(self):
        # kills all inside +30 s -> window is pickup -5 ... +30, no extension
        start, end = common.compute_window(
            pickup_s=100.0, run_end_s=130.004,
            holder_kill_times_s=[101.0, 115.0, 129.9])
        assert start == 95.0
        assert end == 130.0

    def test_extension_when_holder_keeps_fragging_past_30s(self):
        # a kill past pickup+30 extends the window to the end of the run
        start, end = common.compute_window(
            pickup_s=100.0, run_end_s=130.05,
            holder_kill_times_s=[101.0, 130.02])
        assert start == 95.0
        assert end == 130.05

    def test_no_extension_when_run_ends_at_or_before_base_end(self):
        # run ended early (holder died) -> nothing to extend to
        start, end = common.compute_window(
            pickup_s=100.0, run_end_s=125.0,
            holder_kill_times_s=[101.0, 124.0])
        assert end == 130.0

    def test_no_extension_on_zero_kills(self):
        _, end = common.compute_window(
            pickup_s=100.0, run_end_s=130.01, holder_kill_times_s=[])
        assert end == 130.0

    def test_start_clamped_to_match_start(self):
        start, _ = common.compute_window(
            pickup_s=3.0, run_end_s=33.0, holder_kill_times_s=[])
        assert start == 0.0

    def test_end_clamped_to_match_end(self):
        # powerup grabbed in the final seconds must not window past match end
        _, end = common.compute_window(
            pickup_s=1190.0, run_end_s=1220.0, holder_kill_times_s=[1195.0],
            match_end_s=1200.0)
        assert end == 1200.0

    def test_no_end_clamp_when_match_end_unknown(self):
        _, end = common.compute_window(
            pickup_s=1190.0, run_end_s=1220.0, holder_kill_times_s=[1195.0])
        assert end == 1220.0


# -------------------------------------------------------------------- scoring

class TestScoreRun:
    def test_no_bonus_below_two_rl_kills(self):
        assert quad_runner.score_run(7, 0) == 7.0
        assert quad_runner.score_run(7, 1) == 7.0

    def test_bonus_at_two_plus_rl_kills(self):
        assert quad_runner.score_run(4, 2) == 4.5
        assert quad_runner.score_run(6, 3) == 6.75

    def test_higher_frag_count_outranks_rl_styled_run(self):
        # the kippo-demo invariant: 7 frags / 1 RL must beat 6 frags / 3 RL
        assert quad_runner.score_run(7, 1) > quad_runner.score_run(6, 3)


# ------------------------------------------------------------- kill filtering

class TestFilterKills:
    FRAGS = [
        {"time": 1000, "killer": "a", "victim": "x", "weapon": "rl"},
        {"time": 2000, "killer": "a", "victim": "a", "weapon": "rl",
         "isSuicide": True},
        {"time": 3000, "killer": "a", "victim": "mate", "weapon": "lg",
         "isTeamKill": True},
        {"time": 4000, "killer": "b", "victim": "x", "weapon": "lg"},
        {"time": 9000, "killer": "a", "victim": "y", "weapon": "sg"},
    ]

    def test_excludes_suicides_and_teamkills_by_default(self):
        kills = common.filter_kills(self.FRAGS, "a", 0, 5000)
        assert [f["time"] for f in kills] == [1000]

    def test_interval_and_killer_bounds(self):
        kills = common.filter_kills(self.FRAGS, "a", 0, 10_000)
        assert [f["time"] for f in kills] == [1000, 9000]

    def test_opt_in_flags(self):
        kills = common.filter_kills(self.FRAGS, "a", 0, 5000,
                                    include_teamkills=True,
                                    include_suicides=True)
        assert [f["time"] for f in kills] == [1000, 2000, 3000]


# ------------------------------------------------------------------ demo meta

class TestDemoMeta:
    OVERVIEW = {
        "map": "The Abandoned Base",
        "teams": [{"name": "red", "frags": 50}, {"name": "blue", "frags": 40}],
        "playerUserIDs": {"alpha": 11, "beta": 22},
    }

    def test_serverdemo_date_and_short_map(self):
        meta = common.demo_meta(self.OVERVIEW, {"serverInfo": {
            "map": "dm3",
            "serverdemo": "4on4_red_vs_blue[dm3]20260608-2137.mvd",
        }})
        assert meta["map"] == "dm3"
        assert meta["matchdate"] == "2026-06-08"
        assert meta["teams"] == [{"name": "red", "score": 50},
                                 {"name": "blue", "score": 40}]

    def test_epoch_fallback(self):
        meta = common.demo_meta(self.OVERVIEW, {"serverInfo": {
            "epoch": "1780947468"}})  # 2026-06-08 UTC
        assert meta["matchdate"] == "2026-06-08"
        assert meta["map"] == "The Abandoned Base"  # overview fallback


# --------------------------------------------------- mocked end-to-end detect

class FakeClient:
    """Canned mvd-api responses; no network."""

    def __init__(self, overview, metadata, frags, items, events):
        self._data = {"overview": overview, "metadata": metadata,
                      "frags": frags, "items": items, "events": events}
        self.loaded = []

    def load_demo(self, sha):
        self.loaded.append(sha)
        return {"demoId": f"sha:{sha}", "fromCache": True}

    def get_overview(self, sha):
        return self._data["overview"]

    def get_metadata(self, sha):
        return self._data["metadata"]

    def get_frags(self, sha):
        return {"frags": self._data["frags"]}

    def get_items(self, sha, items=None):
        return {"items": self._data["items"]}

    def get_events(self, sha, types=None):
        return {"events": self._data["events"]}


SHA = "ab" * 32


@pytest.fixture
def fake_client():
    overview = {
        "map": "The Abandoned Base",
        "matchStart": 0,
        "matchEnd": 600_000,
        "teams": [{"name": "red", "frags": 50}, {"name": "blue", "frags": 40}],
        "playerUserIDs": {"alpha": 11, "beta": 22},
    }
    metadata = {"serverInfo": {
        "map": "dm3",
        "serverdemo": "4on4_red_vs_blue[dm3]20260608-2137.mvd",
    }}
    frags = [
        # alpha's run (pickup 100 s): 3 scoring kills, 2 of them RL -> bonus
        {"time": 101_000, "killer": "alpha", "victim": "v1", "weapon": "rl"},
        {"time": 105_000, "killer": "alpha", "victim": "v2", "weapon": "rl"},
        {"time": 110_000, "killer": "alpha", "victim": "v3", "weapon": "lg"},
        # beta's run (pickup 200 s): 4 scoring kills, one past +30 s
        {"time": 201_000, "killer": "beta", "victim": "v1", "weapon": "lg"},
        {"time": 205_000, "killer": "beta", "victim": "v2", "weapon": "sg"},
        {"time": 207_000, "killer": "beta", "victim": "beta", "weapon": "rl",
         "isSuicide": True},                                  # excluded
        {"time": 208_000, "killer": "beta", "victim": "mate", "weapon": "rl",
         "isTeamKill": True},                                 # excluded
        {"time": 210_000, "killer": "beta", "victim": "v3", "weapon": "lg"},
        {"time": 230_020, "killer": "beta", "victim": "v4", "weapon": "lg"},
        # noise outside any run
        {"time": 400_000, "killer": "alpha", "victim": "v9", "weapon": "gl"},
    ]
    items = [{
        "name": "quad", "kind": "quad",
        "phases": [
            {"availableFrom": 99_000, "takenAt": 100_000, "takenBy": "alpha",
             "team": "red", "respawnAt": 160_000},
            {"availableFrom": 199_000, "takenAt": 200_000, "takenBy": "beta",
             "team": "blue", "respawnAt": 260_000},
        ],
    }]
    events = [
        {"t": 100.0, "type": "powerup", "player": "alpha",
         "detail": {"powerup": "quad", "endTime": 130.004, "frags": 3}},
        # frags=5 deliberately wrong -> cross-check mismatch warning
        {"t": 200.0, "type": "powerup", "player": "beta",
         "detail": {"powerup": "quad", "endTime": 230.05, "frags": 5}},
    ]
    return FakeClient(overview, metadata, frags, items, events)


class TestDetect:
    def test_ranking_scores_and_contract(self, fake_client, tmp_path):
        cands = quad_runner.detect(fake_client, SHA)
        assert fake_client.loaded == [SHA]
        assert len(cands) == 2

        top, second = cands
        # beta: 4 frags, 0 RL -> 4.0 beats alpha: 3 frags, 2 RL -> 3.5
        assert top["pov_player"] == {"name": "beta", "userid": 22}
        assert top["score"] == 4.0
        assert second["pov_player"] == {"name": "alpha", "userid": 11}
        assert second["score"] == 3.5

        # contract fields + store round-trip (proves contract completeness)
        assert top["category"] == "QUAD RUNNER"
        assert top["demo_sha"] == SHA
        assert top["map"] == "dm3"
        assert top["matchdate"] == "2026-06-08"
        assert top["teams"] == [{"name": "red", "score": 50},
                                {"name": "blue", "score": 40}]
        assert top["hub_url"] == ""
        assert isinstance(top["description"], str) and top["description"]
        db = str(tmp_path / "candidates.db")
        cand_id = store.add_candidate(top, db_path=db)
        assert store.get_candidate(cand_id, db_path=db)["metrics"] == top["metrics"]

    def test_window_extension_and_exclusions_end_to_end(self, fake_client):
        top = quad_runner.detect(fake_client, SHA)[0]
        # beta fragged at 230.02 (> pickup+30=230.0) -> extended to run end
        assert top["window"] == {"start_s": 195.0, "end_s": 230.05}
        m = top["metrics"]
        assert m["frags_in_run"] == 4
        assert m["suicides_excluded"] == 1
        assert m["teamkills_excluded"] == 1
        # per-kill events present, time-ordered, suicide/TK not among them
        assert [k["t_s"] for k in m["kills"]] == [201.0, 205.0, 210.0, 230.02]
        assert all(k["teamkill"] is False for k in m["kills"])
        assert {k["weapon"] for k in m["kills"]} == {"lg", "sg"}

    def test_no_extension_for_run_without_late_frags(self, fake_client):
        second = quad_runner.detect(fake_client, SHA)[1]
        assert second["window"] == {"start_s": 95.0, "end_s": 130.0}
        assert second["metrics"]["rl_kills"] == 2

    def test_crosscheck_mismatch_warns_but_does_not_fail(self, fake_client,
                                                         caplog):
        with caplog.at_level(logging.WARNING,
                             logger="clipsmith.detectors.quad_runner"):
            cands = quad_runner.detect(fake_client, SHA)
        top, second = cands
        assert top["metrics"]["crosscheck"] == {
            "api_frags": 5, "computed": 4, "ok": False}
        assert second["metrics"]["crosscheck"]["ok"] is True
        assert any("frag-count mismatch" in r.message for r in caplog.records)

    def test_min_frags_filter(self, fake_client):
        cands = quad_runner.detect(fake_client, SHA, min_frags=4)
        assert [c["pov_player"]["name"] for c in cands] == ["beta"]


# --------------------------- issue #32: idempotent candidate emission

class TestEmitIdempotency:
    """common.emit_candidate dedup on the documented natural key:
    (category, demo_sha, pov_player.name, window.start_s, window.end_s)."""

    @staticmethod
    def _emit_all(cands, db):
        return [common.emit_candidate(c, db_path=db) for c in cands]

    def test_rescan_emits_no_duplicates_and_same_ids(self, fake_client,
                                                     tmp_path):
        # Issue #32 Measurement: same detection batch twice -> count unchanged.
        db = str(tmp_path / "candidates.db")
        first_ids = self._emit_all(quad_runner.detect(fake_client, SHA), db)
        second_ids = self._emit_all(quad_runner.detect(fake_client, SHA), db)
        assert second_ids == first_ids  # no-op returns the EXISTING ids
        assert len(store.list_candidates(db_path=db)) == len(first_ids) == 2

    def test_crash_mid_emission_rerun_converges(self, fake_client, tmp_path):
        # Crash after a partial emission; a plain rerun must converge to the
        # same store contents as one uninterrupted run.
        db = str(tmp_path / "crashed.db")
        ref_db = str(tmp_path / "ref.db")
        cands = quad_runner.detect(fake_client, SHA)

        common.emit_candidate(cands[0], db_path=db)  # ... then "crash"
        self._emit_all(quad_runner.detect(fake_client, SHA), db)  # rerun
        self._emit_all(cands, ref_db)  # uninterrupted reference

        rows = store.list_candidates(db_path=db)
        ref_rows = store.list_candidates(db_path=ref_db)
        assert len(rows) == len(ref_rows) == len(cands)
        assert ({common.candidate_key(c) for c in rows}
                == {common.candidate_key(c) for c in ref_rows})

    def test_duplicate_is_noop_not_overwrite(self, fake_client, tmp_path):
        # A re-scan must not clobber downstream progress on the stored row.
        db = str(tmp_path / "candidates.db")
        cand = quad_runner.detect(fake_client, SHA)[0]
        cid = common.emit_candidate(cand, db_path=db)
        store.update_status(cid, "posted", db_path=db)
        assert common.emit_candidate(cand, db_path=db) == cid
        assert store.get_candidate(cid, db_path=db)["status"] == "posted"

    def test_same_key_different_metrics_keeps_stored_row(self, fake_client,
                                                         tmp_path):
        # Pin the settled design intent: a re-scan that
        # produces the SAME natural key with DIFFERENT event-specific
        # metrics is still a no-op returning the existing id — the new
        # payload is discarded, the stored row (and any downstream state
        # hanging off it) wins.
        db = str(tmp_path / "candidates.db")
        cand = quad_runner.detect(fake_client, SHA)[0]
        cid = common.emit_candidate(cand, db_path=db)
        stored = store.get_candidate(cid, db_path=db)

        rescanned = dict(cand)
        rescanned["metrics"] = {**cand["metrics"],
                                "frags_in_run": cand["metrics"]["frags_in_run"] + 3}
        assert common.emit_candidate(rescanned, db_path=db) == cid
        assert store.get_candidate(cid, db_path=db)["metrics"] == stored["metrics"]
        assert len(store.list_candidates(db_path=db)) == 1

    def test_different_window_is_a_new_candidate(self, fake_client, tmp_path):
        db = str(tmp_path / "candidates.db")
        cand = quad_runner.detect(fake_client, SHA)[0]
        common.emit_candidate(cand, db_path=db)
        other = dict(cand)
        other["window"] = {"start_s": cand["window"]["start_s"] + 1.0,
                           "end_s": cand["window"]["end_s"] + 1.0}
        common.emit_candidate(other, db_path=db)
        assert len(store.list_candidates(db_path=db)) == 2

    def test_dry_run_does_not_touch_store(self, fake_client, tmp_path,
                                          capsys):
        # Issue #32: --dry-run must be side-effect-free — not even the dedup
        # probe may create the DB file.
        db_path = tmp_path / "candidates.db"
        cand = quad_runner.detect(fake_client, SHA)[0]
        assert common.emit_candidate(cand, db_path=str(db_path),
                                     dry_run=True) is None
        assert not db_path.exists()
        assert json.loads(capsys.readouterr().out)["category"] == "QUAD RUNNER"
