"""Tests for the PENT GRIND detector — window math (pure, offline),
contested-interval bounding, death filtering (proximity + fallbacks), and
top-ranking through a mocked end-to-end detect() run. No network: mvd-api
responses are canned via a fake client."""
import pytest

from clipsmith import store
from clipsmith.detectors import pent_grind


# ---------------------------------------------------------------- window math

class TestComputeGrindWindow:
    def test_long_contest_gets_full_30s_window(self):
        # contest 60 s: window = takenAt -27 ... +3, no clamping
        start, end = pent_grind.compute_grind_window(
            available_from_s=240.0, taken_at_s=300.0)
        assert start == 273.0
        assert end == 303.0

    def test_short_contest_clamps_early_edge_to_available_minus_5(self):
        # contest 0.6 s: availableFrom -5 wins over takenAt -27
        start, end = pent_grind.compute_grind_window(
            available_from_s=902.716, taken_at_s=903.307)
        assert start == pytest.approx(897.716)
        assert end == pytest.approx(906.307)

    def test_contest_exactly_22s_is_the_clamp_boundary(self):
        # availableFrom -5 == takenAt -27 -> both edges agree
        start, _ = pent_grind.compute_grind_window(
            available_from_s=100.0, taken_at_s=122.0)
        assert start == 95.0

    def test_start_clamped_to_match_start(self):
        # pent taken 2.3 s in: window cannot start before the match
        start, end = pent_grind.compute_grind_window(
            available_from_s=0.0, taken_at_s=2.333)
        assert start == 0.0
        assert end == pytest.approx(5.333)

    def test_end_capped_at_match_end(self):
        start, end = pent_grind.compute_grind_window(
            available_from_s=560.0, taken_at_s=599.0, match_end_s=600.0)
        assert start == 572.0
        assert end == 600.0

    def test_end_never_before_start(self):
        # degenerate cap: match ended before the computed start
        start, end = pent_grind.compute_grind_window(
            available_from_s=560.0, taken_at_s=599.0,
            match_start_s=580.0, match_end_s=575.0)
        assert end >= start


# ----------------------------------------------- contested-interval death cut

class TestDeathsInInterval:
    FRAGS = [
        {"time": 999, "killer": "a", "victim": "x", "weapon": "rl"},
        {"time": 1000, "killer": "a", "victim": "y", "weapon": "rl"},
        {"time": 1500, "killer": "y", "victim": "y", "weapon": "lava",
         "isSuicide": True},
        {"time": 2000, "killer": "b", "victim": "mate", "weapon": "rl",
         "isTeamKill": True},
        {"time": 2001, "killer": "c", "victim": "a", "weapon": "lg"},
    ]

    def test_inclusive_bounds(self):
        sel = pent_grind.deaths_in_interval(self.FRAGS, 1000, 2000)
        assert [f["time"] for f in sel] == [1000, 1500, 2000]

    def test_suicides_and_teamkills_included(self):
        sel = pent_grind.deaths_in_interval(self.FRAGS, 0, 3000)
        assert len(sel) == 5  # every obituary is a death — nothing excluded

    def test_time_ordered(self):
        shuffled = list(reversed(self.FRAGS))
        sel = pent_grind.deaths_in_interval(shuffled, 0, 3000)
        assert [f["time"] for f in sel] == sorted(f["time"] for f in self.FRAGS)

    def test_match_start_spawn_telefrags_dropped(self):
        frags = [
            {"time": 0, "killer": "a", "victim": "x", "weapon": "tele"},
            {"time": 0, "killer": "b", "victim": "y", "weapon": "tele"},
            {"time": 50, "killer": "c", "victim": "z", "weapon": "tele"},
        ]
        sel = pent_grind.deaths_in_interval(frags, 0, 2000, match_start_ms=0)
        assert [f["time"] for f in sel] == [50]  # t==matchStart only is cut


# ------------------------------------------------------------------ proximity

class TestEuclid:
    def test_distance(self):
        assert pent_grind.euclid({"x": 3, "y": 4, "z": 0}, (0, 0, 0)) == 5.0

    def test_zero(self):
        assert pent_grind.euclid({"x": 1, "y": 2, "z": 3}, (1, 2, 3)) == 0.0


# --------------------------------------------------- mocked end-to-end detect

class FakeClient:
    """Canned mvd-api responses; no network. Extends the quad-runner fake
    with the generic ``get`` used for /state-at and /demoinfo."""

    def __init__(self, overview, metadata, frags, items,
                 demoinfo=None, state_at=None):
        self._data = {"overview": overview, "metadata": metadata,
                      "frags": frags, "items": items}
        self._demoinfo = demoinfo
        self._state_at = state_at or {}  # time_s -> StateAtView dict
        self.loaded = []
        self.state_at_calls = []

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

    def get(self, sha, path, **params):
        if path == "demoinfo":
            if self._demoinfo is None:
                raise RuntimeError("demoinfo unavailable")
            return self._demoinfo
        if path == "state-at":
            self.state_at_calls.append(params["time"])
            return self._state_at[params["time"]]
        raise AssertionError(f"unexpected path {path!r}")


SHA = "cd" * 32

PENT_XYZ = (1000, 800, -300)


def _state(players):
    return {"players": players}


@pytest.fixture
def fake_client():
    overview = {
        "map": "The Abandoned Base",
        "matchStart": 0,
        "matchEnd": 1_200_000,
        "teams": [{"name": "red", "frags": 50}, {"name": "blue", "frags": 40}],
        "playerUserIDs": {"alpha": 11, "beta": 22, "gamma": 33, "delta": 44},
    }
    metadata = {"serverInfo": {
        "map": "dm3",
        "serverdemo": "4on4_red_vs_blue[dm3]20260608-2137.mvd",
    }}
    demoinfo = {"players": [
        {"name": "alpha", "team": "red"}, {"name": "beta", "team": "blue"},
        {"name": "gamma", "team": "red"}, {"name": "delta", "team": "blue"},
    ]}
    # Phase 1 contest 300.0 -> 310.0 s: 3 deaths, one of them far from pent.
    # Phase 2 contest 600.0 -> 602.0 s: 1 death at pent.
    # Phase 3 untaken. Deaths outside any contest are noise.
    frags = [
        {"time": 299_000, "killer": "alpha", "victim": "beta",
         "weapon": "rl"},                                   # before phase 1
        {"time": 301_000, "killer": "alpha", "victim": "beta", "weapon": "rl"},
        {"time": 305_000, "killer": "gamma", "victim": "gamma",
         "weapon": "lava", "isSuicide": True},              # grind suicide
        {"time": 308_000, "killer": "beta", "victim": "delta", "weapon": "lg",
         "isTeamKill": True},                               # far away -> cut
        {"time": 601_000, "killer": "delta", "victim": "alpha", "weapon": "gl"},
        {"time": 800_000, "killer": "alpha", "victim": "beta",
         "weapon": "sg"},                                   # noise
    ]
    items = [{
        "name": "pent", "kind": "pent",
        "x": PENT_XYZ[0], "y": PENT_XYZ[1], "z": PENT_XYZ[2], "loc": "Pent",
        "phases": [
            {"availableFrom": 300_000, "takenAt": 310_000, "takenBy": "alpha",
             "team": "red", "respawnAt": 610_000},
            {"availableFrom": 600_000, "takenAt": 602_000, "takenBy": "delta",
             "team": "blue", "respawnAt": 902_000},
            {"availableFrom": 902_000, "takenAt": None, "takenBy": None},
        ],
    }]
    state_at = {
        301.0: _state({"beta": {"loc": "Pent",
                                "pos": {"x": 1010, "y": 810, "z": -300}}}),
        305.0: _state({"gamma": {"loc": "Pent",
                                 "pos": {"x": 900, "y": 700, "z": -250}}}),
        308.0: _state({"delta": {"loc": "YA",
                                 "pos": {"x": -900, "y": -800, "z": 0}}}),
        601.0: _state({"alpha": {"loc": "Pent",
                                 "pos": {"x": 1000, "y": 800, "z": -300}}}),
    }
    return FakeClient(overview, metadata, frags, items,
                      demoinfo=demoinfo, state_at=state_at)


class TestDetect:
    def test_ranking_proximity_filter_and_contract(self, fake_client, tmp_path):
        cands = pent_grind.detect(fake_client, SHA)
        assert fake_client.loaded == [SHA]
        assert len(cands) == 2  # untaken phase skipped

        top, second = cands
        # phase 1: 3 in-interval deaths, delta's far-from-pent death cut -> 2
        assert top["score"] == 2
        assert top["pov_player"] == {"name": "alpha", "userid": 11}
        # phase 2: 1 death at pent
        assert second["score"] == 1
        assert second["pov_player"] == {"name": "delta", "userid": 44}

        # contract fields + store round-trip (proves contract completeness)
        assert top["category"] == "PENT GRIND"
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

    def test_death_events_and_team_breakdown(self, fake_client):
        m = pent_grind.detect(fake_client, SHA)[0]["metrics"]
        assert m["deaths_in_interval"] == 3
        assert m["deaths_total"] == 2
        assert m["deaths_by_team"] == {"blue": 1, "red": 1}
        assert m["location_filter"] == "proximity"
        assert m["pent_taker"] == "alpha"
        assert m["pent_loc"] == "Pent"
        assert m["contest_duration_s"] == 10.0

        # per-death events: full detail, time-ordered, far death flagged out
        assert [d["t_s"] for d in m["deaths"]] == [301.0, 305.0, 308.0]
        beta, gamma, delta = m["deaths"]
        assert beta == {"t_s": 301.0, "victim": "beta", "victim_team": "blue",
                        "killer": "alpha", "weapon": "rl", "teamkill": False,
                        "suicide": False, "near_pent": True,
                        "distance": pytest.approx(14.1, abs=0.1),
                        "loc": "Pent"}
        assert gamma["suicide"] is True and gamma["near_pent"] is True
        assert delta["teamkill"] is True and delta["near_pent"] is False

    def test_window_short_contest_clamps_to_available_minus_5(self, fake_client):
        second = pent_grind.detect(fake_client, SHA)[1]
        # contest 600 -> 602: start = max(602-27, 600-5) = 595
        assert second["window"] == {"start_s": 595.0, "end_s": 605.0}

    def test_window_long_contest_full_30s(self, fake_client):
        top = pent_grind.detect(fake_client, SHA)[0]
        # contest 300 -> 310 (10 s): start = max(310-27, 300-5) = 295
        assert top["window"] == {"start_s": 295.0, "end_s": 313.0}

    def test_min_deaths_filter(self, fake_client):
        cands = pent_grind.detect(fake_client, SHA, min_deaths=2)
        assert [c["pov_player"]["name"] for c in cands] == ["alpha"]

    def test_tiebreak_shorter_contest_wins(self, fake_client):
        # widen the radius so all 3 phase-1 deaths count, then shrink
        # phase 1 to tie phase 2 at 1 death via min_deaths trick instead:
        # simplest deterministic check — equal scores rank by duration.
        cands = pent_grind.detect(fake_client, SHA, radius=1.0)
        # radius 1: only alpha's exact-on-pent death (601 s) survives;
        # phase 1 drops to 0 counted deaths and is filtered by min_deaths
        assert len(cands) == 1
        assert cands[0]["pov_player"]["name"] == "delta"
        assert cands[0]["score"] == 1

    def test_tiebreak_on_equal_scores(self):
        # two contests, 1 counted death each; the shorter contest ranks first
        overview = {"map": "dm3", "matchStart": 0, "matchEnd": 1_200_000,
                    "teams": [], "playerUserIDs": {"a": 1, "b": 2}}
        metadata = {"serverInfo": {"map": "dm3"}}
        frags = [
            {"time": 305_000, "killer": "a", "victim": "b", "weapon": "rl"},
            {"time": 601_000, "killer": "b", "victim": "a", "weapon": "rl"},
        ]
        items = [{"name": "pent", "kind": "pent",
                  "x": 0, "y": 0, "z": 0, "loc": "Pent",
                  "phases": [
                      {"availableFrom": 300_000, "takenAt": 310_000,
                       "takenBy": "a", "team": "red"},      # 10 s contest
                      {"availableFrom": 600_000, "takenAt": 602_000,
                       "takenBy": "b", "team": "blue"},     # 2 s contest
                  ]}]
        state_at = {
            305.0: _state({"b": {"loc": "Pent",
                                 "pos": {"x": 0, "y": 0, "z": 0}}}),
            601.0: _state({"a": {"loc": "Pent",
                                 "pos": {"x": 0, "y": 0, "z": 0}}}),
        }
        client = FakeClient(overview, metadata, frags, items,
                            demoinfo={"players": []}, state_at=state_at)
        cands = pent_grind.detect(client, SHA)
        assert [c["score"] for c in cands] == [1, 1]
        assert cands[0]["pov_player"]["name"] == "b"  # 2 s beats 10 s
        assert cands[0]["metrics"]["rank"] == 1
        assert cands[1]["metrics"]["rank"] == 2

    def test_tiebreak_raw_carnage_before_duration(self, fake_client):
        # phase 1: 2 counted + 1 far death (3 in-interval, 10 s contest);
        # craft phase 2 to also count 2 near-pent deaths but with no extra
        # carnage and a shorter contest — phase 1 must still rank first
        # because raw in-interval deaths outrank contest duration.
        fake_client._data["frags"].append(
            {"time": 601_500, "killer": "beta", "victim": "gamma",
             "weapon": "rl"})
        fake_client._state_at[601.5] = _state(
            {"gamma": {"loc": "Pent",
                       "pos": {"x": 1000, "y": 800, "z": -300}}})
        cands = pent_grind.detect(fake_client, SHA)
        assert [c["score"] for c in cands] == [2, 2]
        assert [c["metrics"]["deaths_in_interval"] for c in cands] == [3, 2]
        assert cands[0]["pov_player"]["name"] == "alpha"  # 3 > 2 carnage

    def test_interval_only_fallback_without_coordinates(self, fake_client):
        # strip the pent coordinates -> every in-interval death counts and
        # the simplification is flagged; no state-at calls are made
        fake_client._data["items"][0].pop("x")
        cands = pent_grind.detect(fake_client, SHA)
        top = cands[0]
        assert top["score"] == 3  # delta's far death now counted too
        assert top["metrics"]["location_filter"] == "interval-only"
        assert all(d["near_pent"] is None for d in top["metrics"]["deaths"])
        assert fake_client.state_at_calls == []

    def test_unresolvable_position_is_counted_and_flagged(self, fake_client):
        # state-at knows nothing about beta at 301 s -> death still counts
        fake_client._state_at[301.0] = _state({})
        top = pent_grind.detect(fake_client, SHA)[0]
        assert top["score"] == 2
        beta = top["metrics"]["deaths"][0]
        assert beta["near_pent"] is None and beta["distance"] is None

    def test_demoinfo_failure_degrades_to_empty_teams(self, fake_client):
        fake_client._demoinfo = None
        top = pent_grind.detect(fake_client, SHA)[0]
        assert top["score"] == 2  # detection unaffected
        assert top["metrics"]["deaths_by_team"] == {"?": 2}
        assert all(d["victim_team"] == "" for d in top["metrics"]["deaths"])

    def test_no_pent_items_yields_no_candidates(self, fake_client):
        fake_client._data["items"] = []
        assert pent_grind.detect(fake_client, SHA) == []

    def test_match_start_telefrags_excluded_end_to_end(self, fake_client):
        # first pent phase opens at matchStart; the countdown spawn
        # telefrag at t=0 must not score even though it's near the pent
        fake_client._data["frags"].insert(0, {
            "time": 0, "killer": "alpha", "victim": "beta", "weapon": "tele"})
        fake_client._data["items"][0]["phases"].insert(0, {
            "availableFrom": 0, "takenAt": 10_000, "takenBy": "gamma",
            "team": "red"})
        cands = pent_grind.detect(fake_client, SHA)
        assert "gamma" not in [c["pov_player"]["name"] for c in cands]
        # and no state-at lookup was wasted on the artifact
        assert 0.0 not in fake_client.state_at_calls
