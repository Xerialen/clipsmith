"""Tests for the PENT RUNNER detector — carrier-weighting scoring, the
victim_carrier join (pure, offline), window-extension reuse, suicide/TK
exclusion, ranking, and a mocked end-to-end detect() run.
No network: mvd-api responses are canned via a fake client."""
import logging

import pytest

from clipsmith import store
from clipsmith.detectors import pent_runner


# -------------------------------------------------------------------- scoring

class TestScoreRun:
    def test_carrier_kills_outrank_equal_kill_count(self):
        # issue #8 measurement (1): identical kill counts, 2 carrier kills
        # must outrank 0 carrier kills
        assert pent_runner.score_run(4, 2) > pent_runner.score_run(4, 0)

    def test_bonus_value(self):
        assert pent_runner.score_run(4, 2) == 5.0
        assert pent_runner.score_run(3, 0) == 3.0
        assert pent_runner.score_run(3, 3) == 4.5

    def test_one_extra_raw_frag_at_worst_ties_two_carriers(self):
        # 7 frags / 0 carriers ties 6 frags / 2 carriers on score; the
        # detect() tie-break (raw frag count desc) then favors the 7-frag run
        assert pent_runner.score_run(7, 0) == pent_runner.score_run(6, 2)
        assert pent_runner.score_run(7, 0) > pent_runner.score_run(6, 1)


# ------------------------------------------------------- carrier attribution

class TestVictimCarrier:
    PICKUPS = [
        # x held RL over [10s, 60s] (died at 60s)
        {"time": 10_000, "player": "x", "weapon": "rl",
         "nextDeathTime": 60_000},
        # x held LG over [40s, 60s] too (same death)
        {"time": 40_000, "player": "x", "weapon": "lg",
         "nextDeathTime": 60_000},
        # y picked LG at 5s, never died again (nextDeathTime absent)
        {"time": 5_000, "player": "y", "weapon": "lg"},
        # z's RL interval ended at 30s — gone by a 90s death
        {"time": 20_000, "player": "z", "weapon": "rl",
         "nextDeathTime": 30_000},
        # carrier weapons only: ssg never counts
        {"time": 1_000, "player": "w", "weapon": "ssg",
         "nextDeathTime": 99_000},
    ]
    PACKS = [
        # z dropped an RL pack at his 90s death (backpack corroboration)
        {"time": 90_100, "player": "z", "weapon": "rl"},
    ]

    def test_pickup_interval_covers_death(self):
        assert pent_runner.victim_carrier("y", 50_000, self.PICKUPS, []) == "lg"

    def test_rl_precedence_over_lg(self):
        assert pent_runner.victim_carrier("x", 60_000, self.PICKUPS, []) == "rl"

    def test_outside_interval_is_none(self):
        assert pent_runner.victim_carrier("z", 90_000, self.PICKUPS, []) == "none"

    def test_backpack_corroboration_within_tolerance(self):
        assert pent_runner.victim_carrier(
            "z", 90_000, self.PICKUPS, self.PACKS) == "rl"

    def test_backpack_outside_tolerance_ignored(self):
        assert pent_runner.victim_carrier(
            "z", 95_000, self.PICKUPS, self.PACKS) == "none"

    def test_non_carrier_weapon_never_counts(self):
        assert pent_runner.victim_carrier("w", 50_000, self.PICKUPS, []) == "none"

    def test_unknown_victim(self):
        assert pent_runner.victim_carrier("ghost", 50_000,
                                          self.PICKUPS, self.PACKS) == "none"


# --------------------------------------------------- mocked end-to-end detect

class FakeClient:
    """Canned mvd-api responses; no network. Mirrors common.MvdApiClient's
    surface including the generic .get() used for carrier sources."""

    def __init__(self, overview, metadata, frags, items, events,
                 weapon_pickups=None, backpacks=None,
                 fail_carrier_sources=False):
        self._data = {"overview": overview, "metadata": metadata,
                      "frags": frags, "items": items, "events": events,
                      "weapon-pickups": weapon_pickups or [],
                      "backpacks": backpacks or []}
        self.fail_carrier_sources = fail_carrier_sources
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

    def get(self, sha, path, **params):
        if path in ("weapon-pickups", "backpacks"):
            if self.fail_carrier_sources:
                raise OSError("endpoint down")
            return self._data[path]  # live API returns a bare list
        raise AssertionError(f"unexpected path {path!r}")


SHA = "cd" * 32


@pytest.fixture
def fake_client():
    overview = {
        "map": "The Abandoned Base",
        "matchStart": 0,
        "matchEnd": 1_200_000,
        "teams": [{"name": "red", "frags": 150}, {"name": "blue", "frags": 90}],
        "playerUserIDs": {"alpha": 11, "beta": 22},
    }
    metadata = {"serverInfo": {
        "map": "dm3",
        "serverdemo": "4on4_red_vs_blue[dm3]20260608-2137.mvd",
    }}
    frags = [
        # alpha's run (pickup 100 s): 3 scoring kills, 2 victims were
        # RL/LG carriers -> 3 + 2*0.5 = 4.0
        {"time": 101_000, "killer": "alpha", "victim": "v1", "weapon": "rl"},
        {"time": 105_000, "killer": "alpha", "victim": "v2", "weapon": "sg"},
        {"time": 110_000, "killer": "alpha", "victim": "v3", "weapon": "rl"},
        # beta's run (pickup 400 s): 4 scoring kills (one past +30 s ->
        # window extension), 0 carriers; suicide + TK excluded -> 4.0
        {"time": 401_000, "killer": "beta", "victim": "v1", "weapon": "lg"},
        {"time": 405_000, "killer": "beta", "victim": "v2", "weapon": "sg"},
        {"time": 407_000, "killer": "beta", "victim": "beta", "weapon": "rl",
         "isSuicide": True},                                  # excluded
        {"time": 408_000, "killer": "beta", "victim": "mate", "weapon": "rl",
         "isTeamKill": True},                                 # excluded
        {"time": 410_000, "killer": "beta", "victim": "v3", "weapon": "lg"},
        {"time": 430_020, "killer": "beta", "victim": "v4", "weapon": "lg"},
        # noise outside any run
        {"time": 700_000, "killer": "alpha", "victim": "v9", "weapon": "gl"},
    ]
    items = [{
        "name": "pent", "kind": "pent",
        "phases": [
            {"availableFrom": 99_000, "takenAt": 100_000, "takenBy": "alpha",
             "team": "red", "respawnAt": 400_000},
            {"availableFrom": 399_000, "takenAt": 400_000, "takenBy": "beta",
             "team": "blue", "respawnAt": 700_000},
        ],
    }]
    events = [
        {"t": 100.0, "type": "powerup", "player": "alpha",
         "detail": {"powerup": "pent", "endTime": 130.004, "frags": 3}},
        # frags=6 deliberately wrong -> cross-check mismatch warning
        {"t": 400.0, "type": "powerup", "player": "beta",
         "detail": {"powerup": "pent", "endTime": 430.05, "frags": 6}},
    ]
    weapon_pickups = [
        # v1 held RL over [90s, 101s] -> carrier at alpha's 101s kill
        {"time": 90_000, "player": "v1", "weapon": "rl",
         "nextDeathTime": 101_000},
        # v3 held LG over [95s, 110s] -> carrier at alpha's 110s kill
        {"time": 95_000, "player": "v3", "weapon": "lg",
         "nextDeathTime": 110_000},
        # v2 had only an SSG -> not a carrier
        {"time": 95_000, "player": "v2", "weapon": "ssg",
         "nextDeathTime": 105_000},
        # beta's victims: v1's old RL interval ended LONG before 401s
        {"time": 90_000, "player": "v1", "weapon": "rl",
         "nextDeathTime": 101_000},
    ]
    backpacks = [
        # corroborates v1's RL carrier status at the 101s death
        {"time": 101_050, "player": "v1", "weapon": "rl"},
    ]
    return FakeClient(overview, metadata, frags, items, events,
                      weapon_pickups, backpacks)


class TestDetect:
    def test_carrier_weighting_ranks_equal_kill_counts(self, fake_client,
                                                       tmp_path):
        cands = pent_runner.detect(fake_client, SHA)
        assert fake_client.loaded == [SHA]
        assert len(cands) == 2

        top, second = cands
        # beta: 4 frags / 0 carriers -> 4.0; alpha: 3 frags / 2 carriers
        # -> 4.0. Score tie broken by raw frag count -> beta first.
        assert top["pov_player"] == {"name": "beta", "userid": 22}
        assert top["score"] == 4.0
        assert second["pov_player"] == {"name": "alpha", "userid": 11}
        assert second["score"] == 4.0
        assert second["metrics"]["carrier_kills"] == 2
        assert second["metrics"]["rl_carrier_kills"] == 1
        assert second["metrics"]["lg_carrier_kills"] == 1

        # contract fields + store round-trip (proves contract completeness)
        assert top["category"] == "PENT RUNNER"
        assert top["demo_sha"] == SHA
        assert top["map"] == "dm3"
        assert top["matchdate"] == "2026-06-08"
        assert top["teams"] == [{"name": "red", "score": 150},
                                {"name": "blue", "score": 90}]
        assert top["hub_url"] == ""
        assert isinstance(top["description"], str) and top["description"]
        db = str(tmp_path / "candidates.db")
        cand_id = store.add_candidate(top, db_path=db)
        assert store.get_candidate(cand_id, db_path=db)["metrics"] == top["metrics"]

    def test_per_kill_carrier_flags(self, fake_client):
        alpha = pent_runner.detect(fake_client, SHA)[1]
        kills = alpha["metrics"]["kills"]
        assert [(k["victim"], k["victim_carrier"]) for k in kills] == [
            ("v1", "rl"), ("v2", "none"), ("v3", "lg")]
        assert all(k["teamkill"] is False for k in kills)

    def test_window_extension_and_exclusions_end_to_end(self, fake_client):
        top = pent_runner.detect(fake_client, SHA)[0]
        # beta fragged at 430.02 (> pickup+30=430.0) -> extended to run end
        assert top["window"] == {"start_s": 395.0, "end_s": 430.05}
        m = top["metrics"]
        assert m["frags_in_run"] == 4
        assert m["carrier_kills"] == 0
        assert m["suicides_excluded"] == 1
        assert m["teamkills_excluded"] == 1
        assert [k["t_s"] for k in m["kills"]] == [401.0, 405.0, 410.0, 430.02]

    def test_no_extension_for_run_without_late_frags(self, fake_client):
        second = pent_runner.detect(fake_client, SHA)[1]
        assert second["window"] == {"start_s": 95.0, "end_s": 130.0}

    def test_crosscheck_mismatch_warns_but_does_not_fail(self, fake_client,
                                                         caplog):
        with caplog.at_level(logging.WARNING,
                             logger="clipsmith.detectors.pent_runner"):
            cands = pent_runner.detect(fake_client, SHA)
        top, second = cands
        assert top["metrics"]["crosscheck"] == {
            "api_frags": 6, "computed": 4, "ok": False}
        assert second["metrics"]["crosscheck"]["ok"] is True
        assert any("frag-count mismatch" in r.message for r in caplog.records)

    def test_min_frags_filter(self, fake_client):
        cands = pent_runner.detect(fake_client, SHA, min_frags=4)
        assert [c["pov_player"]["name"] for c in cands] == ["beta"]

    def test_attribution_method_recorded(self, fake_client):
        m = pent_runner.detect(fake_client, SHA)[0]["metrics"]
        attr = m["carrier_attribution"]
        assert attr["method"] == "weapon-pickups+backpacks"
        assert attr["carrier_bonus_per_kill"] == pent_runner.CARRIER_BONUS_PER_KILL


class TestKillWeaponFallback:
    def test_fallback_when_carrier_sources_down(self, fake_client, caplog):
        fake_client.fail_carrier_sources = True
        with caplog.at_level(logging.WARNING,
                             logger="clipsmith.detectors.pent_runner"):
            cands = pent_runner.detect(fake_client, SHA)
        assert any("kill-weapon" in r.message for r in caplog.records)

        top, second = cands
        # fallback weights kills made WITH rl/lg instead:
        # beta 4 frags, 3 lg-weapon kills -> 5.5; alpha 3 frags, 2 rl -> 4.0
        assert top["pov_player"]["name"] == "beta"
        assert top["score"] == 5.5
        assert second["score"] == 4.0
        assert top["metrics"]["carrier_attribution"]["method"] == \
            "kill-weapon-fallback"
        # carrier flags are unknown in fallback mode
        assert all(k["victim_carrier"] is None
                   for k in top["metrics"]["kills"])
