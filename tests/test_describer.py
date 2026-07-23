"""Tests for the candidate describer (issue #10) — one golden-ish test per
category template (key facts asserted, not exact strings), stub detection,
graceful partial-metrics handling, and the store round-trip + CLI.

No network: the metrics dicts are copied verbatim from the real Phase-1
validation candidates (kippo dm3 quad run eb7f447a…, bogojoker schloss
teamkills / Spectator schloss pent run 8d8d69cc…, paniagua dm3 pent grab
eb7f447a…); the store tests use a temp DB.
"""
import copy

import pytest

from clipsmith import describer, store
from clipsmith.describer import describe, looks_like_stub


# ------------------------------------------------- real validation candidates

KIPPO_QUAD = {
    "category": "QUAD RUNNER",
    "demo_sha": "eb7f447a" + "0" * 56,
    "map": "dm3",
    "matchdate": "2026-06-08",
    "teams": [{"name": "-fu-", "score": 210}, {"name": "mix", "score": 94}],
    "pov_player": {"name": "kippo (FU)", "userid": 58},
    "window": {"start_s": 726.269, "end_s": 761.269},
    "score": 7.0,
    "metrics": {
        "quad_pickup_s": 731.269,
        "quad_end_s": 761.273,
        "quad_duration_s": 30.004,
        "frags_in_run": 7,
        "rl_kills": 1,
        "suicides_excluded": 0,
        "teamkills_excluded": 0,
        "kills": [
            {"t_s": 731.35, "weapon": "lg", "victim": "paniagua",
             "teamkill": False},
            {"t_s": 741.222, "weapon": "lg", "victim": "paniagua",
             "teamkill": False},
            {"t_s": 744.956, "weapon": "lg", "victim": "Anza  (FU)",
             "teamkill": False},
            {"t_s": 751.998, "weapon": "lg", "victim": "Anza  (FU)",
             "teamkill": False},
            {"t_s": 752.558, "weapon": "lg", "victim": "paniagua",
             "teamkill": False},
            {"t_s": 756.953, "weapon": "lg", "victim": "paniagua",
             "teamkill": False},
            {"t_s": 758.628, "weapon": "rl", "victim": "Hto   (FU)",
             "teamkill": False},
        ],
        "crosscheck": {"api_frags": 7, "computed": 7, "ok": True},
        "rank": 1,
        "total_quad_runs": 20,
    },
    "description": "kippo (FU)'s quad run on dm3 at 12:11: 7 frags (1 RL).",
    "hub_url": "",
}

BOGOJOKER_TK = {
    "category": "TEAM KILLER",
    "demo_sha": "8d8d69cc" + "0" * 56,
    "map": "schloss",
    "matchdate": "2026-06-10",
    "teams": [{"name": "red", "score": 220}, {"name": "blue", "score": 194}],
    "pov_player": {"name": "bogojoker", "userid": 89},
    "window": {"start_s": 781.2, "end_s": 811.2},
    "score": 2.2207750000000006,
    "metrics": {
        "tk_count": 2,
        "rl_lg_tks": 0,
        "first_tk_s": 795.031,
        "last_tk_s": 797.369,
        "span_s": 2.338,
        "criteria": {"rl_lg_30s": False, "burst_20s": True},
        "source": "frag_delta_reconstruction",
        "kills": [
            {"t_s": 795.031, "weapon": None, "victim": "Venator",
             "teamkill": True},
            {"t_s": 797.369, "weapon": None, "victim": "Venator",
             "teamkill": True},
        ],
        "crosscheck": {"stats_tk": 10, "computed": 8, "ok": False},
        "rank": 1,
        "total_incidents": 3,
    },
    "description": ("bogojoker killed 2 teammates on schloss at 13:15"
                    " within 2 s (0 with RL/LG)."),
    "hub_url": "",
}

PANIAGUA_GRIND = {
    "category": "PENT GRIND",
    "demo_sha": "eb7f447a" + "0" * 56,
    "map": "dm3",
    "matchdate": "2026-06-08",
    "teams": [{"name": "-fu-", "score": 210}, {"name": "mix", "score": 94}],
    "pov_player": {"name": "paniagua", "userid": 54},
    "window": {"start_s": 897.716, "end_s": 906.307},
    "score": 1,
    "metrics": {
        "pent_available_s": 902.716,
        "pent_taken_s": 903.307,
        "contest_duration_s": 0.591,
        "pent_taker": "paniagua",
        "pent_taker_team": "mix",
        "pent_loc": "Pent",
        "deaths_total": 1,
        "deaths_in_interval": 2,
        "deaths_by_team": {"mix": 1},
        "deaths": [
            {"t_s": 902.816, "victim": "Anza  (FU)", "victim_team": "mix",
             "killer": "kippo (FU)", "weapon": "rl", "teamkill": False,
             "suicide": False, "near_pent": False, "distance": 1010.1,
             "loc": "hill"},
            {"t_s": 902.869, "victim": "ToT_Oddjob", "victim_team": "mix",
             "killer": "niomic(FU)", "weapon": "rl", "teamkill": False,
             "suicide": False, "near_pent": True, "distance": 77.8,
             "loc": "Pent"},
        ],
        "location_filter": "proximity",
        "proximity_radius": 768.0,
        "rank": 1,
        "total_pent_phases": 4,
    },
    "description": ("The pent grind on dm3 at 15:03: 1 death(s) in the"
                    " 0.6 s fight before paniagua took it."),
    "hub_url": "",
}

SPECTATOR_PENT = {
    "category": "PENT RUNNER",
    "demo_sha": "8d8d69cc" + "0" * 56,
    "map": "schloss",
    "matchdate": "2026-06-10",
    "teams": [{"name": "red", "score": 220}, {"name": "blue", "score": 194}],
    "pov_player": {"name": "Spectator", "userid": 93},
    "window": {"start_s": 298.116, "end_s": 333.116},
    "score": 8.0,
    "metrics": {
        "pent_pickup_s": 303.116,
        "pent_end_s": 333.117,
        "pent_duration_s": 30.001,
        "frags_in_run": 7,
        "carrier_kills": 2,
        "rl_carrier_kills": 2,
        "lg_carrier_kills": 0,
        "suicides_excluded": 0,
        "teamkills_excluded": 0,
        "kills": [
            {"t_s": 311.615, "weapon": "rl", "victim": "omicron",
             "teamkill": False, "victim_carrier": "rl"},
            {"t_s": 314.36, "weapon": "rl", "victim": "omicron",
             "teamkill": False, "victim_carrier": "none"},
            {"t_s": 315.559, "weapon": "rl", "victim": "Venator",
             "teamkill": False, "victim_carrier": "none"},
            {"t_s": 319.883, "weapon": "sg", "victim": "cronus",
             "teamkill": False, "victim_carrier": "none"},
            {"t_s": 323.14, "weapon": "rl", "victim": "omicron",
             "teamkill": False, "victim_carrier": "none"},
            {"t_s": 327.753, "weapon": "rl", "victim": "bogojoker",
             "teamkill": False, "victim_carrier": "rl"},
            {"t_s": 328.246, "weapon": "rl", "victim": "Venator",
             "teamkill": False, "victim_carrier": "none"},
        ],
        "carrier_attribution": {
            "method": "weapon-pickups+backpacks",
            "weapon_pickups": 245,
            "backpacks": 47,
            "carrier_bonus_per_kill": 0.5,
        },
        "crosscheck": {"api_frags": 7, "computed": 7, "ok": True},
        "rank": 1,
        "total_pent_runs": 4,
    },
    "description": ("Spectator's pent run on schloss at 5:03: 7 frags"
                    " (2 on RL/LG carriers)."),
    "hub_url": "",
}

ALL_REAL = [KIPPO_QUAD, BOGOJOKER_TK, PANIAGUA_GRIND, SPECTATOR_PENT]


# --------------------------------------------------------- category templates

class TestQuadRunnerTemplate:
    def test_kippo_run_key_facts(self):
        d = describe(KIPPO_QUAD)
        # what happens: POV name, kill count, mm:ss pickup, finishing kill
        assert "kippo (FU)" in d
        assert "7 frags" in d
        assert "12:11" in d
        assert "rocket" in d and "Hto" in d  # rl finisher, victim named
        # whose POV + match context
        assert "POV: kippo (FU)" in d
        assert "-fu- vs mix (210-94)" in d
        assert "2026-06-08" in d
        # why recommended: ranking + metrics that ranked it
        assert "highest-scoring quad run" in d
        assert "1 RL kill" in d
        assert "score 7" in d
        assert not looks_like_stub(d)

    def test_few_kills_names_every_victim(self):
        cand = copy.deepcopy(KIPPO_QUAD)
        cand["metrics"]["kills"] = cand["metrics"]["kills"][:2]
        cand["metrics"]["frags_in_run"] = 2
        d = describe(cand)
        assert "paniagua x2" in d  # named (with dedupe), not summarized
        assert "tears through" not in d

    def test_lower_rank_states_position(self):
        cand = copy.deepcopy(KIPPO_QUAD)
        cand["metrics"]["rank"] = 3
        d = describe(cand)
        assert "#3 of 20" in d
        assert "highest-scoring" not in d


class TestTeamKillerTemplate:
    def test_bogojoker_incident_key_facts(self):
        d = describe(BOGOJOKER_TK)
        assert "bogojoker" in d
        assert "2 teammates" in d
        assert "13:15" in d
        assert "Venator x2" in d            # repeat victim deduped
        assert "POV: bogojoker" in d
        assert "red vs blue (220-194)" in d
        assert "multiple teamkills inside 20s" in d
        assert "score 2.22" in d
        # weapons were unrecoverable (reconstruction) -> no weapon claims
        assert "RL/LG" not in d
        assert not looks_like_stub(d)

    def test_rl_lg_incident_mentions_weapons(self):
        cand = copy.deepcopy(BOGOJOKER_TK)
        m = cand["metrics"]
        m["rl_lg_tks"] = 1
        m["criteria"] = {"rl_lg_30s": True, "burst_20s": True}
        m["kills"][0]["weapon"] = "rl"
        d = describe(cand)
        assert "1 with RL/LG" in d
        assert "an RL/LG teamkill" in d

    def test_single_teamkill_wording(self):
        cand = copy.deepcopy(BOGOJOKER_TK)
        m = cand["metrics"]
        m["tk_count"] = 1
        m["kills"] = [{"t_s": 795.031, "weapon": "rl", "victim": "Venator",
                       "teamkill": True}]
        d = describe(cand)
        assert "teammate Venator" in d
        assert "rocket" in d


class TestPentGrindTemplate:
    def test_paniagua_grab_key_facts(self):
        d = describe(PANIAGUA_GRIND)
        assert "paniagua" in d
        assert "15:02" in d                  # pent spawn
        assert "15:03" in d                  # the grab
        assert "ToT_Oddjob" in d             # few deaths -> victim named
        assert "Anza" not in d               # near_pent=False death excluded
        assert "POV: paniagua (mix)" in d
        assert "bloodiest pent contest" in d
        assert "1 death near the pent" in d
        assert "0.6s contest" in d
        assert "score 1" in d
        assert not looks_like_stub(d)

    def test_many_deaths_summarized_by_team(self):
        cand = copy.deepcopy(PANIAGUA_GRIND)
        m = cand["metrics"]
        m["deaths_total"] = 6
        m["deaths_by_team"] = {"mix": 4, "-fu-": 2}
        m["deaths"] = [dict(d, near_pent=True)
                       for d in m["deaths"]] * 3  # 6 counted deaths
        d = describe(cand)
        assert "6 deaths" in d
        assert "4 mix" in d and "2 -fu-" in d
        assert "ToT_Oddjob" not in d.split("POV:")[0].replace(
            "before paniagua", "")  # not individually named in the toll


class TestPentRunnerTemplate:
    def test_spectator_run_key_facts(self):
        d = describe(SPECTATOR_PENT)
        assert "Spectator" in d
        assert "7 frags" in d
        assert "5:03" in d
        assert "rocket" in d and "Venator" in d  # rl finisher
        assert "POV: Spectator" in d
        assert "highest-scoring pent run" in d
        assert "2 kills on RL/LG carriers" in d
        assert "(2 RL)" in d
        assert "score 8" in d
        assert not looks_like_stub(d)


# --------------------------------------------------------------- stub detection

class TestStubDetection:
    @pytest.mark.parametrize("stub", [c["description"] for c in ALL_REAL])
    def test_real_detector_stubs_detected(self, stub):
        assert looks_like_stub(stub)

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_empty_is_stub(self, text):
        assert looks_like_stub(text)

    @pytest.mark.parametrize("cand", ALL_REAL)
    def test_described_text_is_not_stub(self, cand):
        assert not looks_like_stub(describe(cand))

    def test_hand_written_text_is_not_stub(self):
        assert not looks_like_stub("Owner-approved blurb, do not touch.")


# ------------------------------------------------------ graceful degradation

class TestGracefulPartialMetrics:
    @pytest.mark.parametrize("category", sorted(describer.RENDERERS))
    def test_empty_metrics_never_crash(self, category):
        cand = {"category": category, "map": "dm3",
                "pov_player": {"name": "xyz", "userid": 1},
                "teams": [], "metrics": {}}
        d = describe(cand)
        assert isinstance(d, str) and "xyz" in d and "dm3" in d
        assert "POV: xyz" in d and "Why: " in d

    def test_bare_candidate_never_crashes(self):
        d = describe({})
        assert isinstance(d, str) and d

    def test_unknown_category_generic_fallback(self):
        cand = {"category": "COME-BACK", "map": "e1m2", "score": 12,
                "pov_player": {"name": "rikoll"},
                "window": {"start_s": 65.0},
                "metrics": {"whatever": 1}}
        d = describe(cand)
        assert "rikoll" in d and "come-back" in d
        assert "1:05" in d and "score 12" in d

    def test_partial_quad_metrics_describe_what_is_known(self):
        cand = copy.deepcopy(KIPPO_QUAD)
        cand["metrics"] = {"quad_pickup_s": 731.269}  # nothing else survived
        d = describe(cand)
        assert "kippo (FU)" in d and "12:11" in d
        assert "frags" not in d.split("POV:")[0]  # no invented kill counts


# ---------------------------------------------------------------- LLM seam

class TestPolishHook:
    def test_polish_off_by_default_and_deterministic(self):
        assert describe(KIPPO_QUAD) == describe(KIPPO_QUAD)

    def test_polish_hook_wraps_template_output(self):
        seen = {}

        def polish(text, cand):
            seen["text"], seen["cand"] = text, cand
            return "LLM! " + text

        d = describe(KIPPO_QUAD, polish=polish)
        assert d.startswith("LLM! ")
        assert seen["text"] == describer.render_template(KIPPO_QUAD)
        assert seen["cand"] is KIPPO_QUAD


# ------------------------------------------------------- store + CLI round-trip

@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "candidates.db")


class TestStoreRoundTrip:
    def test_describe_candidate_rewrites_description_only(self, db):
        cand_id = store.add_candidate(copy.deepcopy(KIPPO_QUAD), db_path=db)
        text = describer.describe_candidate(cand_id, db_path=db)
        stored = store.get_candidate(cand_id, db_path=db)
        assert stored["description"] == text
        assert not looks_like_stub(stored["description"])
        assert stored["status"] == "candidate"          # untouched
        assert stored["metrics"] == KIPPO_QUAD["metrics"]  # untouched

    def test_dry_run_does_not_write(self, db):
        cand_id = store.add_candidate(copy.deepcopy(BOGOJOKER_TK), db_path=db)
        describer.describe_candidate(cand_id, db_path=db, dry_run=True)
        assert (store.get_candidate(cand_id, db_path=db)["description"]
                == BOGOJOKER_TK["description"])

    def test_set_description_unknown_id_raises(self, db):
        store.add_candidate(copy.deepcopy(KIPPO_QUAD), db_path=db)
        with pytest.raises(ValueError, match="no candidate"):
            store.set_description("nope", "x", db_path=db)

    def test_describe_stubs_skips_hand_written(self, db):
        stub_id = store.add_candidate(copy.deepcopy(SPECTATOR_PENT),
                                      db_path=db)
        custom = copy.deepcopy(PANIAGUA_GRIND)
        custom["description"] = "Owner-approved blurb, do not touch."
        custom_id = store.add_candidate(custom, db_path=db)

        rewritten = describer.describe_stubs(db_path=db)
        assert [cid for cid, _ in rewritten] == [stub_id]
        assert (store.get_candidate(custom_id, db_path=db)["description"]
                == "Owner-approved blurb, do not touch.")
        assert not looks_like_stub(
            store.get_candidate(stub_id, db_path=db)["description"])


class TestCli:
    def test_candidate_mode(self, db, capsys):
        cand_id = store.add_candidate(copy.deepcopy(KIPPO_QUAD), db_path=db)
        describer.main(["--db", db, "--candidate", cand_id])
        out = capsys.readouterr().out
        assert "kippo (FU)" in out and "POV:" in out
        assert (store.get_candidate(cand_id, db_path=db)["description"]
                == out.strip())

    def test_all_stubs_mode_with_status_filter(self, db, capsys):
        stub_id = store.add_candidate(copy.deepcopy(BOGOJOKER_TK), db_path=db)
        posted = copy.deepcopy(SPECTATOR_PENT)
        posted_id = store.add_candidate(posted, db_path=db)
        store.update_status(posted_id, "posted", db_path=db)

        describer.main(["--db", db, "--all-stubs", "--status", "candidate"])
        out = capsys.readouterr().out
        assert stub_id in out and posted_id not in out
        # the posted candidate (filtered out) keeps its stub
        assert looks_like_stub(
            store.get_candidate(posted_id, db_path=db)["description"])

    def test_all_stubs_dry_run(self, db, capsys):
        cand_id = store.add_candidate(copy.deepcopy(PANIAGUA_GRIND),
                                      db_path=db)
        describer.main(["--db", db, "--all-stubs", "--dry-run"])
        assert cand_id in capsys.readouterr().out
        assert looks_like_stub(
            store.get_candidate(cand_id, db_path=db)["description"])

    def test_unknown_candidate_exits_nonzero(self, db):
        with pytest.raises(SystemExit):
            describer.main(["--db", db, "--candidate", "nope"])

    def test_status_filter_rejected_in_candidate_mode(self, db):
        with pytest.raises(SystemExit):
            describer.main(["--db", db, "--candidate", "x",
                            "--status", "candidate"])
