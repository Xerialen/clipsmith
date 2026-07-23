"""Tests for the COME-BACK detector — lead-series reconstruction from
frag-event deltas, the documented trend definition (pure, offline), trend
break, the carrier-kill breaker join, ms/s unit handling, scoring/window
math, and mocked end-to-end detect()/analyze() runs incl. nearest-miss
rejection diagnostics. No network: mvd-api responses are canned via a fake
client."""
import logging

import pytest

from clipsmith import describer, store
from clipsmith.detectors import come_back


# ------------------------------------------------------------------ fit_slope

class TestFitSlope:
    def test_exact_line(self):
        pts = [(t, 3.0 + 0.5 * t) for t in range(0, 10)]
        assert come_back.fit_slope(pts) == pytest.approx(0.5)

    def test_constant_is_zero(self):
        assert come_back.fit_slope([(t, 4.0) for t in range(10)]) == 0.0

    def test_degenerate_input(self):
        assert come_back.fit_slope([]) == 0.0
        assert come_back.fit_slope([(1.0, 5.0)]) == 0.0
        # zero x-variance
        assert come_back.fit_slope([(2.0, 1.0), (2.0, 9.0)]) == 0.0


# ---------------------------------------------------------- build_lead_series

class TestBuildLeadSeries:
    def test_deltas_fold_per_team(self):
        events = [
            {"t": 5.0, "type": "frag", "player": "a1",
             "detail": {"delta": 1, "team": "red"}},
            {"t": 9.0, "type": "frag", "player": "b1",
             "detail": {"delta": 1, "team": "blue"}},
            {"t": 12.0, "type": "frag", "player": "a2",
             "detail": {"delta": 1, "team": "red"}},
            # suicide penalty: red loses one
            {"t": 15.0, "type": "frag", "player": "a1",
             "detail": {"delta": -1, "team": "red"}},
        ]
        assert come_back.build_lead_series(events, "red", "blue") == [
            (5.0, 1), (9.0, 0), (12.0, 1), (15.0, 0)]

    def test_delta_two_teamkill_swing_moves_lead_by_two(self):
        # a delta: 2 frag event (live finding: teamkill swing — the 03b91754
        # histogram has 4 of them) must move the lead by 2, not be clamped
        # to +-1: fold ANY delta
        events = [
            {"t": 5.0, "type": "frag", "player": "a1",
             "detail": {"delta": 1, "team": "red"}},
            {"t": 9.0, "type": "frag", "player": "b1",
             "detail": {"delta": 2, "team": "blue"}},
        ]
        assert come_back.build_lead_series(events, "red", "blue") == [
            (5.0, 1), (9.0, -1)]

    def test_non_frag_and_unknown_team_ignored(self):
        events = [
            {"t": 1.0, "type": "death", "player": "a1"},
            {"t": 2.0, "type": "frag", "player": "x",
             "detail": {"delta": 1, "team": "spectators"}},
            {"t": 3.0, "type": "frag", "player": "a1",
             "detail": {"delta": 1, "team": "red"}},
        ]
        assert come_back.build_lead_series(events, "red", "blue") == [(3.0, 1)]

    def test_unsorted_events_are_sorted(self):
        events = [
            {"t": 10.0, "type": "frag", "player": "b1",
             "detail": {"delta": 1, "team": "blue"}},
            {"t": 4.0, "type": "frag", "player": "a1",
             "detail": {"delta": 1, "team": "red"}},
        ]
        assert come_back.build_lead_series(events, "red", "blue") == [
            (4.0, 1), (10.0, 0)]


# --------------------------------------------------------- sample_step_series

class TestSampleStepSeries:
    def test_step_function_semantics(self):
        bps = [(2.5, 1), (5.0, 3)]
        samples = come_back.sample_step_series(bps, 0.0, 6.0, grid_s=1.0)
        assert samples == [(0.0, 0), (1.0, 0), (2.0, 0), (3.0, 1),
                           (4.0, 1), (5.0, 3), (6.0, 3)]

    def test_empty_breakpoints_all_zero(self):
        samples = come_back.sample_step_series([], 0.0, 3.0, grid_s=1.0)
        assert samples == [(0.0, 0), (1.0, 0), (2.0, 0), (3.0, 0)]

    @pytest.mark.parametrize("bad_grid", [0.0, -1.0])
    def test_non_positive_grid_raises_instead_of_hanging(self, bad_grid):
        with pytest.raises(ValueError, match="grid_s"):
            come_back.sample_step_series([(1.0, 1)], 0.0, 3.0,
                                         grid_s=bad_grid)


# --------------------------------------------------------- find_losing_trends

def ramp_series(rate_per_s, t_end=300, start=1.0):
    """Deficit ramps linearly from ``start`` — clearly behind and sliding."""
    return [(float(t), start + rate_per_s * t) for t in range(0, t_end + 1)]


class TestFindLosingTrends:
    def test_clear_comeback_setup_detected(self):
        # +0.05 frags/s = 3 frags/min for 5 minutes, behind throughout
        trends = come_back.find_losing_trends(ramp_series(0.05))
        assert len(trends) == 1
        t = trends[0]
        assert t["duration_s"] >= come_back.WINDOW_S
        assert t["slope_fpm"] == pytest.approx(3.0, abs=0.01)
        assert t["worst_deficit"] == pytest.approx(16.0)
        assert t["start_s"] == 0.0 and t["end_s"] == 300.0

    def test_steady_deficit_rejected(self):
        # behind by a constant 5 for 5 minutes: behind but NOT sliding
        flat = [(float(t), 5.0) for t in range(0, 301)]
        assert come_back.find_losing_trends(flat) == []

    def test_brief_blip_rejected(self):
        # behind by 1 all game; a 20 s burst to +4 then back — the late
        # spike satisfies net+slope over a trailing window but is not a
        # 2-minute trend (half-window condition 4 rejects it)
        blip = [(float(t), 4.0 if 110 <= t <= 130 else 1.0)
                for t in range(0, 301)]
        assert come_back.find_losing_trends(blip) == []

    def test_leading_team_never_qualifies(self):
        # "deficit" falling from -1 to -16: the team is AHEAD
        trends = come_back.find_losing_trends(ramp_series(0.05, start=-17.0))
        assert trends == []

    def test_tie_mid_window_disqualifies(self):
        # rises 0.05/s but the game was tied (deficit 0) at t=150
        series = [(float(t), 0.0 if t == 150 else 1.0 + 0.05 * t)
                  for t in range(0, 301)]
        trends = come_back.find_losing_trends(series)
        # windows spanning t=150 are out; trends exist only clear of it
        assert all(not (tr["start_s"] <= 150.0 <= tr["end_s"])
                   for tr in trends)

    def test_slow_creep_below_net_increase_rejected(self):
        # +1 frag per 2 minutes: rising, but immaterial (net < 2)
        creep = ramp_series(1.0 / 120.0)
        assert come_back.find_losing_trends(creep) == []

    def test_one_slide_with_stall_is_one_merged_trend(self):
        # ramp (1/16 frag/s) for 120 s, stall 70 s, ramp again for 120 s:
        # the stall opens a gap in qualifying times (half-window condition
        # fails over the flat part) but the trailing windows overlap, so
        # this is ONE slide, not two near-duplicate trends
        r = 1.0 / 16.0  # binary-exact rate: no fp jitter at thresholds
        def v(t):
            if t <= 120:
                return 1.0 + r * t
            if t <= 190:
                return 1.0 + r * 120
            return 1.0 + r * 120 + r * (t - 190)
        series = [(float(t), v(t)) for t in range(0, 311)]
        trends = come_back.find_losing_trends(series)
        assert len(trends) == 1
        assert trends[0]["start_s"] == 0.0
        assert trends[0]["end_s"] == 310.0

    def test_non_positive_window_returns_empty(self):
        # window_s <= 0 must not divide-by-zero or produce phantom trends
        assert come_back.find_losing_trends(ramp_series(0.05),
                                            window_s=0.0) == []
        assert come_back.find_losing_trends(ramp_series(0.05),
                                            window_s=-5.0) == []

    def test_degenerate_zero_spacing_grid_returns_empty(self):
        # duplicate grid timestamps -> grid spacing 0: guarded, not a
        # ZeroDivisionError
        samples = [(10.0, float(v)) for v in range(1, 200)]
        assert come_back.find_losing_trends(samples) == []

    def test_parameters_are_tunable(self):
        # +1 frag per 100 s: rejected at defaults (net < 2 over the
        # window) but accepted when the thresholds are relaxed
        creep = ramp_series(1.0 / 100.0)
        assert come_back.find_losing_trends(creep) == []
        trends = come_back.find_losing_trends(
            creep, min_net_increase=1, half_min_net=0.5, min_slope_fpm=0.4)
        assert len(trends) == 1


# ---------------------------------------------------------- find_trend_breaks

class TestFindTrendBreaks:
    def test_break_at_end_of_peak_plateau(self):
        # rises to 8, holds 8 over t=[200, 210], then falls: the break is
        # the LAST attainment of the peak (end of the plateau)
        series = ([(float(t), t / 25.0) for t in range(0, 201)]
                  + [(float(t), 8.0) for t in range(201, 211)]
                  + [(float(t), 8.0 - (t - 210) / 5.0)
                     for t in range(211, 241)])
        breaks = come_back.find_trend_breaks(series, 0.0, 240.0)
        assert breaks == [{"break_t_s": 210.0, "worst_deficit": 8.0}]

    def test_no_break_when_deficit_never_recedes(self):
        series = [(float(t), t / 25.0) for t in range(0, 301)]
        assert come_back.find_trend_breaks(series, 0.0, 300.0) == []

    def test_each_material_reversal_is_a_break(self):
        # dent at t=100, re-worsens to a second peak at t=150, falls again:
        # BOTH reversals are breaks (the live 8d8d demo pattern)
        vals = {}
        for t in range(0, 201):
            if t <= 100:
                vals[t] = t / 10.0          # 0 -> 10
            elif t <= 125:
                vals[t] = 10.0 - (t - 100) / 5.0   # 10 -> 5
            elif t <= 150:
                vals[t] = 5.0 + (t - 125) / 5.0    # 5 -> 10
            else:
                vals[t] = 10.0 - (t - 150) / 5.0   # 10 -> 0
        series = [(float(t), vals[t]) for t in range(0, 201)]
        breaks = come_back.find_trend_breaks(series, 0.0, 200.0)
        assert breaks == [{"break_t_s": 100.0, "worst_deficit": 10.0},
                          {"break_t_s": 150.0, "worst_deficit": 10.0}]

    def test_dip_below_min_recovery_is_not_a_break(self):
        # rises to 10, dips 1 (immaterial), new high 12, then collapses:
        # only the 12-peak is a break
        series = ([(float(t), t / 10.0) for t in range(0, 101)]
                  + [(float(t), 9.0) for t in range(101, 111)]
                  + [(float(t), 12.0) for t in range(111, 121)]
                  + [(float(t), 12.0 - (t - 120) / 2.0)
                     for t in range(121, 141)])
        breaks = come_back.find_trend_breaks(series, 0.0, 140.0)
        assert breaks == [{"break_t_s": 120.0, "worst_deficit": 12.0}]

    def test_scan_range_is_respected(self):
        series = [(float(t), 10.0 if t < 50 else 5.0) for t in range(0, 101)]
        # the fall at t=50 is outside [60, 100]: no break there
        assert come_back.find_trend_breaks(series, 60.0, 100.0) == []


# --------------------------------------------------------- collect_break_kills

TEAM_OF = {"hero": "blue", "bluemate": "blue",
           "v_red1": "red", "v_red2": "red", "redguy": "red"}

PICKUPS = [
    {"time": 150_000, "player": "v_red1", "weapon": "rl",
     "nextDeathTime": 200_000},
    {"time": 150_000, "player": "v_red2", "weapon": "lg",
     "nextDeathTime": 205_000},
]


class TestCollectBreakKills:
    FRAGS = [
        # in-window enemy kills by the behind team (ms!)
        {"time": 200_000, "killer": "hero", "victim": "v_red1",
         "weapon": "rl"},
        {"time": 205_000, "killer": "hero", "victim": "v_red2",
         "weapon": "sg"},
        # suicide and teamkill near the break: excluded
        {"time": 206_000, "killer": "hero", "victim": "hero",
         "weapon": "rl", "isSuicide": True},
        {"time": 207_000, "killer": "hero", "victim": "bluemate",
         "weapon": "rl", "isTeamKill": True},
        # outside the join window: excluded
        {"time": 260_000, "killer": "hero", "victim": "v_red1",
         "weapon": "rl"},
        # opponent's kill: not the behind team
        {"time": 201_000, "killer": "redguy", "victim": "bluemate",
         "weapon": "rl"},
    ]

    def test_ms_to_s_join_and_filters(self):
        rows = come_back.collect_break_kills(
            self.FRAGS, TEAM_OF, "blue", "red", break_t_s=199.0,
            join_s=20.0, weapon_pickups=PICKUPS, backpacks=[])
        assert [(r["t_s"], r["victim"]) for r in rows] == [
            (200.0, "v_red1"), (205.0, "v_red2")]
        assert [r["victim_carrier"] for r in rows] == ["rl", "lg"]
        assert all(r["is_carrier_kill"] for r in rows)

    def test_spawn_telefrag_at_match_start_dropped(self):
        frags = [{"time": 0, "killer": "hero", "victim": "v_red1",
                  "weapon": "tele"},
                 {"time": 4_000, "killer": "hero", "victim": "v_red2",
                  "weapon": "rl"}]
        rows = come_back.collect_break_kills(
            frags, TEAM_OF, "blue", "red", break_t_s=5.0, join_s=20.0,
            match_start_ms=0, weapon_pickups=[], backpacks=[])
        assert [r["t_s"] for r in rows] == [4.0]

    def test_kill_weapon_fallback(self):
        rows = come_back.collect_break_kills(
            self.FRAGS, TEAM_OF, "blue", "red", break_t_s=199.0,
            join_s=20.0, attribution_ok=False)
        assert [(r["weapon"], r["is_carrier_kill"], r["victim_carrier"])
                for r in rows] == [("rl", True, None), ("sg", False, None)]


# ----------------------------------------------------------------- find_breaker

def kill_row(t_s, killer, carrier=True, victim="v"):
    return {"t_s": t_s, "killer": killer, "victim": victim,
            "weapon": "rl", "victim_carrier": "rl" if carrier else "none",
            "is_carrier_kill": carrier}


class TestFindBreaker:
    def test_two_carrier_kills_by_one_player(self):
        rows = [kill_row(200.0, "hero"), kill_row(205.0, "hero"),
                kill_row(202.0, "bluemate", carrier=False)]
        out = come_back.find_breaker(rows)
        assert out["breaker"]["player"] == "hero"
        assert out["breaker"]["carrier_kills"] == 2
        assert out["breaker"]["first_carrier_t_s"] == 200.0

    def test_split_carrier_kills_no_breaker(self):
        rows = [kill_row(200.0, "hero"), kill_row(205.0, "bluemate")]
        out = come_back.find_breaker(rows)
        assert out["breaker"] is None
        # nearest-miss summary still ranks the players
        assert [p["carrier_kills"] for p in out["players"]] == [1, 1]

    def test_best_breaker_most_carriers_then_total_then_earliest(self):
        rows = [kill_row(210.0, "a"), kill_row(215.0, "a"),
                kill_row(200.0, "b"), kill_row(205.0, "b"),
                kill_row(208.0, "b", carrier=False)]
        out = come_back.find_breaker(rows)
        assert out["breaker"]["player"] == "b"   # 2+2 ties, b has 3 total


# ------------------------------------------------------------ score and window

class TestScoreAndWindow:
    def test_carrier_kills_dominate(self):
        assert come_back.score_comeback(3, 5) > come_back.score_comeback(2, 9)

    def test_deficit_breaks_ties(self):
        assert come_back.score_comeback(2, 10) > come_back.score_comeback(2, 5)
        assert come_back.score_comeback(2, 10) == pytest.approx(3.0)

    def test_window_minus5_plus30(self):
        assert come_back.compute_break_window(200.0) == (195.0, 230.0)

    def test_window_clamps_to_match_bounds(self):
        assert come_back.compute_break_window(3.0) == (0.0, 33.0)
        assert come_back.compute_break_window(
            590.0, match_end_s=600.0) == (585.0, 600.0)


# --------------------------------------------------- mocked end-to-end detect

class FakeClient:
    """Canned mvd-api responses; no network. Mirrors common.MvdApiClient's
    surface including the generic .get() used for carrier sources."""

    def __init__(self, overview, metadata, frags, events,
                 weapon_pickups=None, backpacks=None,
                 fail_carrier_sources=False):
        self._data = {"overview": overview, "metadata": metadata,
                      "frags": frags, "events": events,
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

    def get_events(self, sha, types=None):
        return {"events": self._data["events"]}

    def get(self, sha, path, **params):
        if path in ("weapon-pickups", "backpacks"):
            if self.fail_carrier_sources:
                raise OSError("endpoint down")
            return self._data[path]  # live API returns a bare list
        raise AssertionError(f"unexpected path {path!r}")


SHA = "ab" * 32

OVERVIEW = {
    "map": "The Abandoned Base",
    "matchStart": 0,
    "matchEnd": 600_000,
    "teams": [{"name": "red", "frags": 180}, {"name": "blue", "frags": 175}],
    "players": [
        {"name": "redguy", "team": "red", "frags": 60},
        {"name": "v_red1", "team": "red", "frags": 60},
        {"name": "v_red2", "team": "red", "frags": 60},
        {"name": "hero", "team": "blue", "frags": 90},
        {"name": "bluemate", "team": "blue", "frags": 85},
    ],
    "playerUserIDs": {"hero": 7, "bluemate": 8, "redguy": 9,
                      "v_red1": 10, "v_red2": 11},
}
METADATA = {"serverInfo": {
    "map": "dm3",
    "serverdemo": "4on4_red_vs_blue[dm3]20260608-2137.mvd",
}}


def comeback_events():
    """red pulls ahead +1 every 12 s from t=10 to t=190 (16 frags, ~5
    frags/min, blue behind throughout), then blue claws back from t=200."""
    events = [
        {"t": float(t), "type": "frag", "player": "redguy",
         "detail": {"delta": 1, "team": "red"}}
        for t in range(10, 191, 12)
    ]
    for t, player in ((200, "hero"), (205, "hero"), (210, "bluemate"),
                      (215, "bluemate"), (220, "bluemate"),
                      (230, "bluemate")):
        events.append({"t": float(t), "type": "frag", "player": player,
                       "detail": {"delta": 1, "team": "blue"}})
    return events


COMEBACK_FRAGS = [
    # noise early in the match
    {"time": 22_000, "killer": "redguy", "victim": "bluemate",
     "weapon": "rl"},
    # the breaking play: hero kills both enemy carriers (ms!)
    {"time": 200_000, "killer": "hero", "victim": "v_red1", "weapon": "rl"},
    {"time": 205_000, "killer": "hero", "victim": "v_red2", "weapon": "sg"},
    # excluded from the play: suicide + teamkill near the break
    {"time": 206_000, "killer": "hero", "victim": "hero", "weapon": "rl",
     "isSuicide": True},
    {"time": 207_000, "killer": "hero", "victim": "bluemate", "weapon": "rl",
     "isTeamKill": True},
    # bluemate helps but kills a non-carrier
    {"time": 210_000, "killer": "bluemate", "victim": "redguy",
     "weapon": "sg"},
]

COMEBACK_PICKUPS = [
    {"time": 150_000, "player": "v_red1", "weapon": "rl",
     "nextDeathTime": 200_000},
    {"time": 150_000, "player": "v_red2", "weapon": "lg",
     "nextDeathTime": 205_000},
]
COMEBACK_PACKS = [
    {"time": 200_050, "player": "v_red1", "weapon": "rl"},
]


@pytest.fixture
def comeback_client():
    return FakeClient(OVERVIEW, METADATA, COMEBACK_FRAGS, comeback_events(),
                      COMEBACK_PICKUPS, COMEBACK_PACKS)


class TestDetect:
    def test_clear_comeback_detected(self, comeback_client, tmp_path):
        cands = come_back.detect(comeback_client, SHA)
        assert comeback_client.loaded == [SHA]
        assert len(cands) == 1
        c = cands[0]

        assert c["category"] == "COMEBACK"
        assert c["pov_player"] == {"name": "hero", "userid": 7}
        m = c["metrics"]
        assert m["behind_team"] == "blue"
        assert m["opponent_team"] == "red"
        assert m["lead_at_worst"] == 16
        assert m["break_t_s"] == 199.0     # last grid second at the worst
        assert m["carrier_kills"] == 2
        assert m["rl_carrier_kills"] == 1
        assert m["lg_carrier_kills"] == 1
        assert m["trend_start_s"] == 10.0
        assert m["trend_duration_s"] >= come_back.WINDOW_S
        assert m["trend_to_break_s"] == 189.0   # slide start -> break
        assert 3.0 < m["trend_slope_fpm"] < 7.0
        # score = carrier_kills + 0.1 * lead_at_worst
        assert c["score"] == pytest.approx(2 + 0.1 * 16)
        # window: first carrier kill (200 s) -5 ... +30
        assert c["window"] == {"start_s": 195.0, "end_s": 230.0}
        # the suicide/TK near the break never enter the play
        assert [(k["victim"], k["victim_carrier"]) for k in m["kills"]] == [
            ("v_red1", "rl"), ("v_red2", "lg")]

        # contract fields + store round-trip (proves contract completeness)
        assert c["demo_sha"] == SHA
        assert c["map"] == "dm3"
        assert c["matchdate"] == "2026-06-08"
        assert c["teams"] == [{"name": "red", "score": 180},
                              {"name": "blue", "score": 175}]
        assert c["hub_url"] == ""
        db = str(tmp_path / "candidates.db")
        cand_id = store.add_candidate(c, db_path=db)
        assert store.get_candidate(cand_id, db_path=db)["metrics"] == m

    def test_description_is_digest_quality_not_stub(self, comeback_client):
        c = come_back.detect(comeback_client, SHA)[0]
        # hand-written full description: must survive emit untouched
        assert not describer.looks_like_stub(c["description"])
        for fragment in ("hero", "blue", "dm3", "16", "POV:", "Why:"):
            assert fragment in c["description"]

    def test_even_game_yields_nothing(self):
        # teams trade frags evenly: nobody is ever 2+ behind for 2 min
        events = []
        for t in range(10, 591, 20):
            events.append({"t": float(t), "type": "frag", "player": "redguy",
                           "detail": {"delta": 1, "team": "red"}})
            events.append({"t": float(t + 10), "type": "frag",
                           "player": "hero",
                           "detail": {"delta": 1, "team": "blue"}})
        client = FakeClient(OVERVIEW, METADATA, [], events)
        result = come_back.analyze(client, SHA)
        assert result["trends_found"] == 0
        assert result["candidates"] == []
        assert result["rejections"] == []

    def test_steady_slide_without_recovery_rejected_no_break(self):
        # red +1 every 12 s all game; blue never recovers
        events = [
            {"t": float(t), "type": "frag", "player": "redguy",
             "detail": {"delta": 1, "team": "red"}}
            for t in range(10, 591, 12)
        ]
        client = FakeClient(OVERVIEW, METADATA, [], events)
        result = come_back.analyze(client, SHA)
        assert result["candidates"] == []
        assert result["trends_found"] == 1
        assert [r["stage"] for r in result["rejections"]] == ["no_break"]
        assert result["rejections"][0]["behind_team"] == "blue"

    def test_comeback_without_carrier_kills_rejected(self, tmp_path):
        # same trend + recovery, but the victims carried nothing
        client = FakeClient(OVERVIEW, METADATA, COMEBACK_FRAGS,
                            comeback_events(), weapon_pickups=[],
                            backpacks=[])
        result = come_back.analyze(client, SHA)
        assert result["candidates"] == []
        rej = [r for r in result["rejections"] if r["stage"] == "no_breaker"]
        assert len(rej) == 1
        # nearest-miss diagnostics name the best player and his count
        nearest = rej[0]["nearest_players"]
        assert nearest[0]["player"] == "hero"
        assert nearest[0]["carrier_kills"] == 0
        assert nearest[0]["total_kills"] == 2

    def test_carrier_sources_down_falls_back_to_kill_weapon(
            self, comeback_client, caplog):
        comeback_client.fail_carrier_sources = True
        with caplog.at_level(logging.WARNING,
                             logger="clipsmith.detectors.come_back"):
            result = come_back.analyze(comeback_client, SHA)
        assert any("kill-weapon" in r.message for r in caplog.records)
        # hero's play: rl kill counts, sg kill doesn't -> only 1 "carrier"
        # kill in fallback -> below MIN_CARRIER_KILLS -> rejected
        assert result["candidates"] == []
        rej = [r for r in result["rejections"] if r["stage"] == "no_breaker"]
        assert rej and rej[0]["nearest_players"][0]["carrier_kills"] == 1
        # the attribution method must be flagged when a candidate IS
        # produced: lower the bar to 1 carrier kill
        result = come_back.analyze(comeback_client, SHA,
                                   min_carrier_kills=1)
        c = result["candidates"][0]
        assert c["metrics"]["carrier_attribution"]["method"] == \
            "kill-weapon-fallback"
        assert all(k["victim_carrier"] is None
                   for k in c["metrics"]["kills"])

    def test_non_two_team_demo_yields_nothing(self, caplog):
        overview = dict(OVERVIEW, teams=[{"name": "ffa", "frags": 10}])
        client = FakeClient(overview, METADATA, [], [])
        with caplog.at_level(logging.WARNING,
                             logger="clipsmith.detectors.come_back"):
            result = come_back.analyze(client, SHA)
        assert result["candidates"] == []
        assert any("exactly 2 teams" in r.message for r in caplog.records)

    @pytest.mark.parametrize("kwargs", [
        {"grid_s": 0.0}, {"grid_s": -1.0},
        {"window_s": 0.0}, {"window_s": -120.0},
    ])
    def test_non_positive_grid_or_window_rejected(self, comeback_client,
                                                  kwargs):
        # validated at the analyze boundary BEFORE any API call: a zero
        # grid would divide by zero / hang the resampler
        with pytest.raises(ValueError):
            come_back.analyze(comeback_client, SHA, **kwargs)
        assert comeback_client.loaded == []

    @pytest.mark.parametrize("argv", [
        ["--sha", SHA, "--grid-s", "0"],
        ["--sha", SHA, "--grid-s", "-1"],
        ["--sha", SHA, "--window-s", "0"],
        ["--sha", SHA, "--window-s", "-120"],
    ])
    def test_cli_rejects_non_positive_grid_and_window(self, argv, capsys):
        with pytest.raises(SystemExit):
            come_back.main(argv)
        err = capsys.readouterr().err
        assert "must be > 0" in err

    def test_tunable_parameters_flow_through(self, comeback_client):
        # window stretched beyond the slide: nothing can qualify
        cands = come_back.detect(comeback_client, SHA, window_s=400.0)
        assert cands == []
        # stricter slope than the ~5 fpm slide: nothing qualifies
        cands = come_back.detect(comeback_client, SHA, min_slope_fpm=10.0)
        assert cands == []
        # params are recorded in metrics
        c = come_back.detect(comeback_client, SHA, break_join_s=25.0)[0]
        assert c["metrics"]["params"]["break_join_s"] == 25.0
        assert c["metrics"]["params"]["window_s"] == come_back.WINDOW_S
