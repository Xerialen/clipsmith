"""Tests for the TEAM KILLER detector — criteria branches, clustering gap
behavior, window centring, scoring, the frag-delta reconstruction fallback,
and mocked end-to-end detect() runs. No network: mvd-api responses are
canned via a fake client (pattern from tests/test_quad_runner.py)."""
import logging

import pytest

from clipsmith import store
from clipsmith.detectors import team_killer


# --------------------------------------------------------------- clustering

class TestClusterTeamkills:
    def _k(self, t_s, weapon="sg", victim="mate"):
        return {"t_s": t_s, "weapon": weapon, "victim": victim}

    def test_single_cluster_within_gap(self):
        kills = [self._k(10.0), self._k(25.0), self._k(44.0)]
        clusters = team_killer.cluster_teamkills(kills)
        assert [[k["t_s"] for k in c] for c in clusters] == [[10.0, 25.0, 44.0]]

    def test_split_on_gap_over_threshold(self):
        kills = [self._k(10.0), self._k(30.1), self._k(35.0)]
        clusters = team_killer.cluster_teamkills(kills)
        assert [[k["t_s"] for k in c] for c in clusters] == [
            [10.0], [30.1, 35.0]]

    def test_gap_exactly_at_threshold_stays_joined(self):
        kills = [self._k(10.0), self._k(30.0)]
        assert len(team_killer.cluster_teamkills(kills)) == 1

    def test_unsorted_input_is_sorted(self):
        kills = [self._k(44.0), self._k(10.0), self._k(25.0)]
        clusters = team_killer.cluster_teamkills(kills)
        assert [k["t_s"] for k in clusters[0]] == [10.0, 25.0, 44.0]


# ----------------------------------------------------------------- criteria

class TestIncidentCriteria:
    def _k(self, t_s, weapon):
        return {"t_s": t_s, "weapon": weapon, "victim": "mate"}

    def test_single_rl_tk_qualifies_criterion_a(self):
        c = team_killer.incident_criteria([self._k(10.0, "rl")])
        assert c == {"rl_lg_30s": True, "burst_20s": False}

    def test_single_lg_tk_qualifies_criterion_a(self):
        assert team_killer.incident_criteria(
            [self._k(10.0, "lg")])["rl_lg_30s"] is True

    def test_single_shotgun_tk_qualifies_neither(self):
        c = team_killer.incident_criteria([self._k(10.0, "sg")])
        assert c == {"rl_lg_30s": False, "burst_20s": False}

    def test_two_tks_within_20s_qualify_criterion_b(self):
        c = team_killer.incident_criteria(
            [self._k(10.0, "sg"), self._k(29.9, "axe")])
        assert c["burst_20s"] is True

    def test_burst_found_in_sub_window_of_wider_cluster(self):
        # chain 0..38 s is one cluster; the 19-s pair satisfies the burst
        c = team_killer.incident_criteria(
            [self._k(0.0, "sg"), self._k(19.0, "sg"), self._k(38.0, "sg")])
        assert c["burst_20s"] is True

    def test_reconstructed_kills_without_weapon_cannot_hit_criterion_a(self):
        c = team_killer.incident_criteria(
            [self._k(10.0, None), self._k(15.0, None)])
        assert c == {"rl_lg_30s": False, "burst_20s": True}


# ------------------------------------------------------------------ scoring

class TestScoreIncident:
    def test_more_tks_in_less_time_ranks_higher(self):
        tight = team_killer.score_incident(3, 0, 5.0)
        loose = team_killer.score_incident(3, 0, 18.0)
        assert tight > loose

    def test_count_dominates_all_bonuses(self):
        # 3 RL TKs in 0 s (max bonuses) must NOT outrank a plain 4-TK sprawl
        maxed_3 = team_killer.score_incident(3, 3, 0.0)
        plain_4 = team_killer.score_incident(4, 0, 20.0)
        assert maxed_3 < plain_4

    def test_weapon_bonus(self):
        assert team_killer.score_incident(1, 1, 0.0) == pytest.approx(1.15)
        assert team_killer.score_incident(1, 0, 0.0) == 1.0

    def test_single_tk_gets_no_compactness_bonus(self):
        assert team_killer.score_incident(1, 0, 0.0) == 1.0
        # but a pair at span 0 does
        assert team_killer.score_incident(2, 0, 0.0) == pytest.approx(2.25)


# ----------------------------------------------------------- window centring

class TestComputeIncidentWindow:
    def test_centred_on_single_kill(self):
        start, end = team_killer.compute_incident_window(
            100.0, 100.0, 0.0, 1200.0)
        assert (start, end) == (85.0, 115.0)

    def test_centred_on_cluster_midpoint(self):
        start, end = team_killer.compute_incident_window(
            100.0, 110.0, 0.0, 1200.0)
        assert (start, end) == (90.0, 120.0)

    def test_clamp_at_match_start_preserves_length(self):
        start, end = team_killer.compute_incident_window(5.0, 5.0, 0.0, 1200.0)
        assert (start, end) == (0.0, 30.0)

    def test_clamp_at_match_end_preserves_length(self):
        start, end = team_killer.compute_incident_window(
            1198.0, 1198.0, 0.0, 1200.0)
        assert (start, end) == (1170.0, 1200.0)

    def test_short_match_shrinks_window(self):
        start, end = team_killer.compute_incident_window(10.0, 10.0, 0.0, 20.0)
        assert (start, end) == (0.0, 20.0)

    def test_no_match_end_known(self):
        start, end = team_killer.compute_incident_window(1198.0, 1198.0, 0.0,
                                                         None)
        assert (start, end) == (1183.0, 1213.0)


# ------------------------------------------------- frag-log TK extraction

class TestTeamkillsFromFragLog:
    def test_selects_only_teamkills_excluding_suicides(self):
        frags = [
            {"time": 1000, "killer": "a", "victim": "x", "weapon": "rl"},
            {"time": 2000, "killer": "a", "victim": "mate", "weapon": "rl",
             "isTeamKill": True},
            {"time": 3000, "killer": "a", "victim": "a", "weapon": "rl",
             "isSuicide": True, "isTeamKill": True},
            {"time": 4000, "killer": "b", "victim": "mate2", "weapon": "sg",
             "isTeamKill": True},
        ]
        by_killer = team_killer.teamkills_from_frag_log(frags)
        assert by_killer == {
            "a": [{"t_s": 2.0, "weapon": "rl", "victim": "mate"}],
            "b": [{"t_s": 4.0, "weapon": "sg", "victim": "mate2"}],
        }


# ------------------------------------------------------------ reconstruction

class TestReconstructTeamkills:
    TEAM_OF = {"a": "red", "mate": "red", "enemy": "blue", "b": "blue"}

    def test_penalty_with_teammate_death_becomes_tk(self):
        events = [
            {"t": 50.0, "type": "frag", "player": "a",
             "detail": {"delta": -1, "team": "red"}},
            {"t": 50.2, "type": "death", "player": "mate"},
        ]
        out = team_killer.reconstruct_teamkills([], events, self.TEAM_OF)
        assert out == {"a": [{"t_s": 50.0, "weapon": None, "victim": "mate"}]}

    def test_penalty_matching_kill_log_suicide_is_not_a_tk(self):
        frags = [{"time": 50_100, "killer": "a", "victim": "a",
                  "weapon": "suicide", "isSuicide": True}]
        events = [
            {"t": 50.0, "type": "frag", "player": "a",
             "detail": {"delta": -1, "team": "red"}},
            {"t": 50.0, "type": "death", "player": "a"},
        ]
        out = team_killer.reconstruct_teamkills(frags, events, self.TEAM_OF)
        assert out == {}

    def test_ambiguous_victim_is_none(self):
        team_of = dict(self.TEAM_OF, mate2="red")
        events = [
            {"t": 50.0, "type": "frag", "player": "a",
             "detail": {"delta": -1, "team": "red"}},
            {"t": 50.1, "type": "death", "player": "mate"},
            {"t": 50.2, "type": "death", "player": "mate2"},
        ]
        out = team_killer.reconstruct_teamkills([], events, team_of)
        assert out["a"][0]["victim"] is None

    def test_enemy_death_is_not_a_victim_candidate(self):
        events = [
            {"t": 50.0, "type": "frag", "player": "a",
             "detail": {"delta": -1, "team": "red"}},
            {"t": 50.1, "type": "death", "player": "enemy"},
        ]
        out = team_killer.reconstruct_teamkills([], events, self.TEAM_OF)
        assert out["a"][0]["victim"] is None

    def test_double_penalty_yields_two_tks(self):
        events = [
            {"t": 50.0, "type": "frag", "player": "a",
             "detail": {"delta": -2, "team": "red"}},
        ]
        out = team_killer.reconstruct_teamkills([], events, self.TEAM_OF)
        assert len(out["a"]) == 2

    def test_positive_deltas_ignored(self):
        events = [{"t": 50.0, "type": "frag", "player": "a",
                   "detail": {"delta": 1, "team": "red"}}]
        assert team_killer.reconstruct_teamkills([], events, self.TEAM_OF) == {}


# --------------------------------------------------- mocked end-to-end detect

class FakeClient:
    """Canned mvd-api responses; no network."""

    def __init__(self, overview, metadata, frags, events=None, demoinfo=None):
        self._data = {"overview": overview, "metadata": metadata,
                      "frags": frags, "events": events or [],
                      "demoinfo": demoinfo}
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

    def get_events(self, sha, types=None):
        return {"events": self._data["events"]}

    def get(self, sha, path, **params):
        assert path == "demoinfo"
        if self._data["demoinfo"] is None:
            import urllib.error
            raise urllib.error.HTTPError(
                "http://test/demoinfo", 404, "not found", None, None)
        return self._data["demoinfo"]


SHA = "ab" * 32

OVERVIEW = {
    "map": "The Abandoned Base",
    "matchStart": 0,
    "matchEnd": 1_200_000,
    "teams": [{"name": "red", "frags": 50}, {"name": "blue", "frags": 40}],
    "playerUserIDs": {"alpha": 11, "beta": 22, "gamma": 33},
}
METADATA = {"serverInfo": {
    "map": "dm3",
    "serverdemo": "4on4_red_vs_blue[dm3]20260608-2137.mvd",
}}


def demoinfo(tk_by_name, team_by_name=None):
    team_by_name = team_by_name or {}
    return {"players": [
        {"name": n, "team": team_by_name.get(n, "red"),
         "stats": {"tk": tk}} for n, tk in tk_by_name.items()
    ]}


@pytest.fixture
def frag_log_client():
    """Frag-log tier: isTeamKill entries present, both criteria covered."""
    frags = [
        # alpha: single RL teamkill -> criterion (a)
        {"time": 100_000, "killer": "alpha", "victim": "mate1",
         "weapon": "rl", "isTeamKill": True},
        # beta: 3-TK shotgun burst inside 20 s -> criterion (b)
        {"time": 300_000, "killer": "beta", "victim": "mate1",
         "weapon": "sg", "isTeamKill": True},
        {"time": 308_000, "killer": "beta", "victim": "mate2",
         "weapon": "sg", "isTeamKill": True},
        {"time": 315_000, "killer": "beta", "victim": "mate3",
         "weapon": "axe", "isTeamKill": True},
        # gamma: single shotgun TK -> neither criterion (negative case)
        {"time": 500_000, "killer": "gamma", "victim": "mate1",
         "weapon": "sg", "isTeamKill": True},
        # plain enemy kill: never a teamkill candidate
        {"time": 600_000, "killer": "alpha", "victim": "enemy",
         "weapon": "rl"},
        # suicide flagged as TK must not count
        {"time": 700_000, "killer": "alpha", "victim": "alpha",
         "weapon": "rl", "isSuicide": True, "isTeamKill": True},
    ]
    info = demoinfo({"alpha": 1, "beta": 3, "gamma": 1})
    return FakeClient(OVERVIEW, METADATA, frags, demoinfo=info)


class TestDetectFragLog:
    def test_both_criteria_qualify_and_negative_case_does_not(
            self, frag_log_client):
        cands = team_killer.detect(frag_log_client, SHA)
        assert frag_log_client.loaded == [SHA]
        names = [c["pov_player"]["name"] for c in cands]
        assert "gamma" not in names           # single SG TK: neither criterion
        assert set(names) == {"alpha", "beta"}

    def test_ranking_and_scores(self, frag_log_client):
        top, second = team_killer.detect(frag_log_client, SHA)
        # beta: 3 TKs, span 15 s -> 3 + 0 + 0.25*(1-15/20) = 3.0625
        assert top["pov_player"] == {"name": "beta", "userid": 22}
        assert top["score"] == pytest.approx(3.0625)
        assert top["metrics"]["criteria"] == {"rl_lg_30s": False,
                                              "burst_20s": True}
        # alpha: 1 TK, 1 RL -> 1.15
        assert second["pov_player"] == {"name": "alpha", "userid": 11}
        assert second["score"] == pytest.approx(1.15)
        assert second["metrics"]["criteria"] == {"rl_lg_30s": True,
                                                 "burst_20s": False}

    def test_window_centred_on_cluster(self, frag_log_client):
        top, second = team_killer.detect(frag_log_client, SHA)
        # beta's cluster 300..315 s -> centre 307.5 -> 292.5..322.5
        assert top["window"] == {"start_s": 292.5, "end_s": 322.5}
        # alpha's single TK at 100 s -> 85..115
        assert second["window"] == {"start_s": 85.0, "end_s": 115.0}

    def test_contract_fields_and_store_round_trip(self, frag_log_client,
                                                  tmp_path):
        top = team_killer.detect(frag_log_client, SHA)[0]
        assert top["category"] == "TEAM KILLER"
        assert top["demo_sha"] == SHA
        assert top["map"] == "dm3"
        assert top["matchdate"] == "2026-06-08"
        assert top["teams"] == [{"name": "red", "score": 50},
                                {"name": "blue", "score": 40}]
        assert top["hub_url"] == ""
        assert isinstance(top["description"], str) and top["description"]
        m = top["metrics"]
        assert m["source"] == "frag_log"
        assert m["kills"] == [
            {"t_s": 300.0, "weapon": "sg", "victim": "mate1", "teamkill": True},
            {"t_s": 308.0, "weapon": "sg", "victim": "mate2", "teamkill": True},
            {"t_s": 315.0, "weapon": "axe", "victim": "mate3",
             "teamkill": True},
        ]
        db = str(tmp_path / "candidates.db")
        cand_id = store.add_candidate(top, db_path=db)
        assert store.get_candidate(cand_id, db_path=db)["metrics"] == m

    def test_crosscheck_ok_and_mismatch_warns(self, frag_log_client, caplog):
        # stats.tk agrees for alpha/beta/gamma -> ok; now break beta's
        frag_log_client._data["demoinfo"] = demoinfo(
            {"alpha": 1, "beta": 5, "gamma": 1})
        with caplog.at_level(logging.WARNING,
                             logger="clipsmith.detectors.team_killer"):
            top, second = team_killer.detect(frag_log_client, SHA)
        assert top["metrics"]["crosscheck"] == {
            "stats_tk": 5, "computed": 3, "ok": False}
        assert second["metrics"]["crosscheck"] == {
            "stats_tk": 1, "computed": 1, "ok": True}
        assert any("teamkill-count mismatch" in r.message
                   for r in caplog.records)

    def test_no_demoinfo_skips_reconciliation(self, frag_log_client):
        frag_log_client._data["demoinfo"] = None
        top = team_killer.detect(frag_log_client, SHA)[0]
        assert top["metrics"]["crosscheck"] == {
            "stats_tk": None, "computed": 3, "ok": True}

    def test_min_tks_relaxed_mode_emits_negative_case(self, frag_log_client):
        cands = team_killer.detect(frag_log_client, SHA, min_tks=1)
        names = [c["pov_player"]["name"] for c in cands]
        assert "gamma" in names
        gamma = next(c for c in cands if c["pov_player"]["name"] == "gamma")
        assert gamma["metrics"]["criteria"] == {"rl_lg_30s": False,
                                                "burst_20s": False}

    def test_clustering_gap_splits_incidents_end_to_end(self):
        # two RL TKs 25 s apart -> two separate criterion-(a) incidents
        frags = [
            {"time": 100_000, "killer": "alpha", "victim": "mate1",
             "weapon": "rl", "isTeamKill": True},
            {"time": 125_000, "killer": "alpha", "victim": "mate2",
             "weapon": "rl", "isTeamKill": True},
        ]
        client = FakeClient(OVERVIEW, METADATA, frags,
                            demoinfo=demoinfo({"alpha": 2}))
        cands = team_killer.detect(client, SHA)
        assert len(cands) == 2
        assert all(c["metrics"]["tk_count"] == 1 for c in cands)
        assert all(c["metrics"]["total_incidents"] == 2 for c in cands)


class TestDetectReconstructionFallback:
    @pytest.fixture
    def fallback_client(self):
        """No isTeamKill frags; stats.tk > 0 -> reconstruction tier.

        cronus-style incident: 3 frag-delta penalties within 18 s, each
        paired with a same-team death.
        """
        frags = [
            {"time": 10_000, "killer": "alpha", "victim": "enemy",
             "weapon": "rl"},
            # suicide explaining one of alpha's penalties (t=400 s)
            {"time": 400_000, "killer": "alpha", "victim": "alpha",
             "weapon": "suicide", "isSuicide": True},
        ]
        events = [
            # alpha's TK burst: 280, 290, 298 s
            {"t": 280.0, "type": "frag", "player": "alpha",
             "detail": {"delta": -1, "team": "red"}},
            {"t": 280.2, "type": "death", "player": "mate1"},
            {"t": 290.0, "type": "frag", "player": "alpha",
             "detail": {"delta": -1, "team": "red"}},
            {"t": 290.1, "type": "death", "player": "mate2"},
            {"t": 298.0, "type": "frag", "player": "alpha",
             "detail": {"delta": -1, "team": "red"}},
            {"t": 298.3, "type": "death", "player": "mate1"},
            # alpha's suicide penalty (matched by the kill log -> not a TK)
            {"t": 400.0, "type": "frag", "player": "alpha",
             "detail": {"delta": -1, "team": "red"}},
            {"t": 400.0, "type": "death", "player": "alpha"},
        ]
        info = {"players": [
            {"name": "alpha", "team": "red", "stats": {"tk": 3}},
            {"name": "mate1", "team": "red", "stats": {"tk": 0}},
            {"name": "mate2", "team": "red", "stats": {"tk": 0}},
            {"name": "beta", "team": "blue", "stats": {"tk": 0}},
        ]}
        return FakeClient(OVERVIEW, METADATA, frags, events=events,
                          demoinfo=info)

    def test_reconstructed_burst_qualifies_criterion_b(self, fallback_client,
                                                       caplog):
        with caplog.at_level(logging.WARNING,
                             logger="clipsmith.detectors.team_killer"):
            cands = team_killer.detect(fallback_client, SHA)
        assert len(cands) == 1
        c = cands[0]
        assert c["pov_player"]["name"] == "alpha"
        m = c["metrics"]
        assert m["source"] == "frag_delta_reconstruction"
        assert m["tk_count"] == 3
        assert m["rl_lg_tks"] == 0          # weapons unknown
        assert m["criteria"] == {"rl_lg_30s": False, "burst_20s": True}
        assert [k["victim"] for k in m["kills"]] == ["mate1", "mate2", "mate1"]
        assert all(k["weapon"] is None and k["teamkill"] is True
                   for k in m["kills"])
        # window centred on 280..298 -> 274..304
        assert c["window"] == {"start_s": 274.0, "end_s": 304.0}
        # reconciliation: computed 3 == stats.tk 3 -> ok (suicide excluded)
        assert m["crosscheck"] == {"stats_tk": 3, "computed": 3, "ok": True}
        assert any("falling back to frag-delta reconstruction" in r.message
                   for r in caplog.records)

    def test_no_fallback_when_stats_tk_zero(self):
        client = FakeClient(OVERVIEW, METADATA, [], events=[],
                            demoinfo=demoinfo({"alpha": 0}))
        assert team_killer.detect(client, SHA) == []
