"""Tests for clipsmith.publish_metadata — golden files per category
(byte-identical), char-limit assertions (title <= 100, description <= 5000,
hook <= 157 and self-contained), required description elements, file
placement next to the MP4, week derivation, CLI smoke. Temp DBs only,
no network.

Golden files live in tests/golden/publish_metadata/ (LF-only; protected
from autocrlf by the .gitattributes there). Regenerate after an intended
template change with:

    python -m tests.test_publish_metadata --regen
"""
import json
from pathlib import Path

import pytest

from clipsmith import publish_metadata as pm
from clipsmith import store

GOLDEN_DIR = Path(__file__).parent / "golden" / "publish_metadata"

WEEK = "2026-W24"  # Mon 2026-06-08 .. Sun 2026-06-14


def base_candidate(cand_id, category, metrics, description, score=9.0):
    """A fixed, fully-populated store-schema candidate (golden input)."""
    return {
        "id": cand_id,
        "category": category,
        "detected_at": "2026-06-10T13:12:38+00:00",
        "demo_sha": "ab" * 32,
        "map": "dm3",
        "matchdate": "2026-06-09",
        "teams": [{"name": "tot", "score": 187}, {"name": "-s-", "score": 142}],
        "pov_player": {"name": "xerial", "userid": 42},
        "window": {"start_s": 312.4, "end_s": 347.9},
        "score": score,
        "metrics": metrics,
        "description": description,
        "hub_url": "https://hub.quakeworld.nu/games/?gameId=221227&from=322&track=42",
        "status": "rendered",
    }


GOLDEN_CANDIDATES = {
    "quad_runner": base_candidate(
        "golden-quad-runner", "QUAD RUNNER",
        {"quad_pickup_s": 317.4, "frags_in_run": 5, "rl_kills": 3,
         "rank": 1, "total_quad_runs": 14,
         "kills": [{"t_s": 319.2, "weapon": "rl", "victim": "bps",
                    "teamkill": False}]},
        "xerial grabs quad at 5:17 on dm3 and tears through 5 frags in 21s, "
        "finishing with a rocket on bps. POV: xerial — tot vs -s- (187-142), "
        "2026-06-09. Why: the highest-scoring quad run in this demo "
        "(14 runs) — 5 frags incl. 3 RL kills, score 9."),
    "team_killer": base_candidate(
        "golden-team-killer", "TEAM KILLER",
        {"tk_count": 3, "first_tk_s": 412.0, "span_s": 11.5, "rl_lg_tks": 2,
         "rank": 1, "total_incidents": 4,
         "criteria": {"burst_20s": True, "rl_lg_30s": True},
         "kills": [{"t_s": 412.0, "weapon": "rl", "victim": "mate1",
                    "teamkill": True}]},
        "xerial takes out 3 teammates on dm3 in 12s starting at 6:52, 2 with "
        "RL/LG. POV: xerial — tot vs -s- (187-142), 2026-06-09. Why: the "
        "worst teamkill incident in this demo (4 incidents) — 3 teamkills "
        "in 11.5s, qualifies via multiple teamkills inside 20s + an RL/LG "
        "teamkill, score 9.", score=6.0),
    "pent_grind": base_candidate(
        "golden-pent-grind", "PENT GRIND",
        {"pent_available_s": 300.0, "pent_taken_s": 341.2,
         "contest_duration_s": 41.2, "deaths_total": 6,
         "pent_taker": "xerial", "pent_taker_team": "tot",
         "deaths_by_team": {"tot": 2, "-s-": 4},
         "rank": 1, "total_pent_phases": 5,
         "deaths": [{"t_s": 305.1, "victim": "bps", "near_pent": True}]},
        "The pent on dm3 spawns at 5:00 and, over the next 41.2s, the fight "
        "for it claims 6 deaths (2 tot, 4 -s-) before xerial grabs it at "
        "5:41. POV: xerial (tot) — tot vs -s- (187-142), 2026-06-09. Why: "
        "the bloodiest pent contest in this demo (5 contests) — 6 deaths "
        "near the pent in the 41.2s contest, score 9.", score=6.0),
    "pent_runner": base_candidate(
        "golden-pent-runner", "PENT RUNNER",
        {"pent_pickup_s": 317.4, "frags_in_run": 4, "carrier_kills": 2,
         "rl_carrier_kills": 1, "lg_carrier_kills": 1,
         "rank": 1, "total_pent_runs": 3,
         "kills": [{"t_s": 320.0, "weapon": "rl", "victim": "bps",
                    "teamkill": False}]},
        "xerial grabs pent at 5:17 on dm3 and tears through 4 frags in 25s, "
        "finishing with the shaft on bps. POV: xerial — tot vs -s- "
        "(187-142), 2026-06-09. Why: the highest-scoring pent run in this "
        "demo (3 runs) — 4 frags incl. 2 kills on RL/LG carriers "
        "(1 RL, 1 LG), score 9.", score=7.5),
}


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "candidates.db")


# ------------------------------------------------------------ golden files

@pytest.mark.parametrize("name", sorted(GOLDEN_CANDIDATES))
def test_golden_file_byte_identical(name):
    """Fixed fixture -> byte-identical committed metadata JSON (issue #27
    Measurement). On an INTENDED template change, regenerate with
    `python -m tests.test_publish_metadata --regen` and review the diff."""
    golden = GOLDEN_DIR / f"{name}.metadata.json"
    generated = pm.to_json(pm.generate(GOLDEN_CANDIDATES[name]))
    assert golden.read_bytes() == generated.encode("utf-8")


@pytest.mark.parametrize("name", sorted(GOLDEN_CANDIDATES))
def test_golden_metadata_is_valid_json_with_required_keys(name):
    meta = json.loads((GOLDEN_DIR / f"{name}.metadata.json")
                      .read_text(encoding="utf-8"))
    assert {"candidate_id", "title", "description",
            "hashtags", "tags"} <= set(meta)
    assert meta["candidate_id"] == GOLDEN_CANDIDATES[name]["id"]


# ------------------------------------------------------------- char limits

@pytest.mark.parametrize("name", sorted(GOLDEN_CANDIDATES))
def test_char_limits(name):
    cand = GOLDEN_CANDIDATES[name]
    meta = pm.generate(cand)
    assert len(meta["title"]) <= pm.TITLE_MAX
    assert len(meta["description"]) <= pm.DESCRIPTION_MAX
    assert len(meta["hook"]) <= pm.HOOK_MAX
    # tags box budget: comma-joined <= 500 chars; hashtag ignore-all cliff
    assert len(", ".join(meta["tags"])) <= pm.TAGS_MAX_CHARS
    assert len(meta["hashtags"]) <= pm.HASHTAGS_MAX


@pytest.mark.parametrize("name", sorted(GOLDEN_CANDIDATES))
def test_hook_is_frontloaded_and_self_contained(name):
    meta = pm.generate(GOLDEN_CANDIDATES[name])
    # the hook IS the first paragraph: everything before the fold
    assert meta["description"].startswith(meta["hook"] + "\n\n")
    # self-contained: a complete sentence naming the category + week
    assert meta["hook"].endswith(".")
    assert GOLDEN_CANDIDATES[name]["category"] in meta["hook"]
    assert "Week 24" in meta["hook"]


def test_title_pattern():
    meta = pm.generate(GOLDEN_CANDIDATES["quad_runner"])
    assert meta["title"] == "QUAD RUNNER OF THE WEEK — Week 24 — xerial (dm3)"


def test_title_truncates_long_names_gracefully():
    cand = dict(GOLDEN_CANDIDATES["quad_runner"])
    cand["pov_player"] = {"name": "x" * 90, "userid": 1}
    cand["map"] = "m" * 40
    title = pm.build_title(cand, WEEK)
    assert len(title) <= pm.TITLE_MAX
    # fixed pattern parts survive; names got the ellipsis
    assert title.startswith("QUAD RUNNER OF THE WEEK — Week 24 — ")
    assert title.endswith(")")
    assert "…" in title


@pytest.mark.parametrize("name", sorted(GOLDEN_CANDIDATES))
def test_hook_long_names_never_cut_the_suffix(name):
    """Only the variable event clause may be ellipsized — the
    fixed '— CATEGORY OF THE WEEK, Week NN.' tail survives any name."""
    cand = dict(GOLDEN_CANDIDATES[name])
    cand["pov_player"] = {"name": "p" * 200, "userid": 1}
    cand["map"] = "m" * 60
    cand["metrics"] = dict(cand["metrics"], pent_taker="t" * 80)
    hook = pm.build_hook(cand, WEEK)
    assert len(hook) <= pm.HOOK_MAX
    assert hook.endswith(f" — {cand['category']} OF THE WEEK, Week 24.")
    assert "…" in hook  # the event clause took the cut


def test_hook_long_names_generic_category_keeps_suffix():
    cand = dict(GOLDEN_CANDIDATES["quad_runner"])
    cand["category"] = "COME-BACK"  # no template -> generic fallback
    cand["pov_player"] = {"name": "p" * 200, "userid": 1}
    hook = pm.build_hook(cand, WEEK)
    assert len(hook) <= pm.HOOK_MAX
    assert hook.endswith(" — COME-BACK OF THE WEEK, Week 24.")


def test_description_truncates_oversized_body_not_links(monkeypatch):
    cand = dict(GOLDEN_CANDIDATES["quad_runner"])
    cand["description"] = "x" * 6000
    desc = pm.build_description(cand, WEEK)
    assert len(desc) <= pm.DESCRIPTION_MAX
    # links and hashtags survive the trim
    for required in (pm.DISCORD_URL, pm.WIKI_URL, pm.NQUAKE_URL,
                     pm.CHANNEL_HANDLE_URL, pm.CHANNEL_ID_URL,
                     pm.PLAYLIST_URL, "#QuakeWorld"):
        assert required in desc
    assert "…" in desc


# ------------------------------------------------- required content elements

@pytest.mark.parametrize("name", sorted(GOLDEN_CANDIDATES))
def test_description_contains_required_elements_in_order(name):
    cand = GOLDEN_CANDIDATES[name]
    desc = pm.generate(cand)["description"]
    # the spec's elements, in order: hook (event+player), candidate
    # description, Discord, wiki, nQuake, channel cross-links
    positions = [
        desc.index(cand["pov_player"]["name"]),       # the player (hook)
        desc.index(cand["description"][:40]),          # store description
        desc.index(pm.DISCORD_URL),
        desc.index(pm.WIKI_URL),
        desc.index(pm.NQUAKE_URL),
        desc.index(pm.PLAYLIST_URL),
        desc.index(pm.CHANNEL_HANDLE_URL),
        desc.index(pm.CHANNEL_ID_URL),
    ]
    assert positions == sorted(positions)
    assert cand["hub_url"] in desc
    # hashtags embedded in the description
    for tag in pm.build_hashtags(cand):
        assert tag in desc


def test_hashtags_lead_with_searchable_then_category():
    tags = pm.build_hashtags(GOLDEN_CANDIDATES["pent_grind"])
    assert tags[:3] == ["#QuakeWorld", "#Quake", "#PentGrind"]


def test_empty_hub_url_omits_hub_line():
    cand = dict(GOLDEN_CANDIDATES["quad_runner"])
    cand["hub_url"] = ""
    desc = pm.build_description(cand, WEEK)
    assert "Relive the moment" not in desc
    assert pm.DISCORD_URL in desc


# --------------------------------------- category extensibility (COME-BACK)

def test_unknown_category_uses_generic_fallback():
    cand = dict(GOLDEN_CANDIDATES["quad_runner"])
    cand["category"] = "COME-BACK"
    meta = pm.generate(cand)
    assert meta["title"].startswith("COME-BACK OF THE WEEK — Week 24 — ")
    assert "COME-BACK OF THE WEEK, Week 24." in meta["hook"]
    assert len(meta["hook"]) <= pm.HOOK_MAX
    assert "#ComeBack" in meta["hashtags"]  # derived, no template needed


def test_come_back_slots_in_with_one_template_entry(monkeypatch):
    # Hook-builder contract: (cand, metrics) -> the variable event clause
    # only; build_hook appends the fixed "— CATEGORY OF THE WEEK, Week NN."
    monkeypatch.setitem(
        pm.CATEGORY_TEMPLATES, "COME-BACK",
        {"hook": lambda cand, m:
            f"{cand['pov_player']['name']} breaks the slide on {cand['map']}",
         "hashtag": "#ComeBack"})
    cand = dict(GOLDEN_CANDIDATES["quad_runner"])
    cand["category"] = "COME-BACK"
    meta = pm.generate(cand)
    assert meta["hook"] == ("xerial breaks the slide on dm3 — "
                            "COME-BACK OF THE WEEK, Week 24.")


# ----------------------------------------------------------- week derivation

def test_week_from_matchdate_governs():
    cand = dict(GOLDEN_CANDIDATES["quad_runner"])
    cand["matchdate"] = "2026-06-09"
    cand["detected_at"] = "2026-06-20T00:00:00+00:00"
    assert pm.week_for_candidate(cand) == "2026-W24"


def test_week_falls_back_to_detected_at():
    cand = dict(GOLDEN_CANDIDATES["quad_runner"])
    cand["matchdate"] = ""
    cand["detected_at"] = "2026-06-16T12:00:00+00:00"
    assert pm.week_for_candidate(cand) == "2026-W25"


def test_week_unresolvable_raises():
    cand = dict(GOLDEN_CANDIDATES["quad_runner"])
    cand["matchdate"] = ""
    cand["detected_at"] = "garbage"
    with pytest.raises(ValueError, match="--week"):
        pm.week_for_candidate(cand)


def test_explicit_week_override():
    meta = pm.generate(GOLDEN_CANDIDATES["quad_runner"], week_id="2026-W30")
    assert meta["week"] == "2026-W30"
    assert "Week 30" in meta["title"]


def test_generate_rejects_bad_week():
    with pytest.raises(ValueError):
        pm.generate(GOLDEN_CANDIDATES["quad_runner"], week_id="junk")


# ------------------------------------------------------------- file placement

def test_metadata_path_next_to_mp4():
    assert pm.metadata_path_for("renders/clip-abc.mp4") == Path(
        "renders/clip-abc.metadata.json")


def test_write_metadata_lf_only(tmp_path):
    meta = pm.generate(GOLDEN_CANDIDATES["quad_runner"])
    mp4 = tmp_path / "golden-quad-runner.mp4"
    path = pm.write_metadata(meta, pm.metadata_path_for(mp4))
    assert path == tmp_path / "golden-quad-runner.metadata.json"
    raw = path.read_bytes()
    assert b"\r" not in raw                       # LF-only on any host
    assert json.loads(raw.decode("utf-8")) == meta  # round-trips


# -------------------------------------------------------------- CLI smoke

def test_cli_writes_next_to_mp4(db, tmp_path, capsys):
    store.add_candidate(GOLDEN_CANDIDATES["quad_runner"], db_path=db)
    mp4 = tmp_path / "clip.mp4"
    pm.main(["--db", db, "--candidate", "golden-quad-runner",
             "--mp4", str(mp4)])
    out_file = tmp_path / "clip.metadata.json"
    assert out_file.exists()
    meta = json.loads(out_file.read_text(encoding="utf-8"))
    assert meta["candidate_id"] == "golden-quad-runner"
    assert "metadata written" in capsys.readouterr().err


def test_cli_stdout_mode_and_status_note(db, capsys):
    """No --mp4/--out -> documented preview mode: JSON on stdout, notes on
    stderr (stdout stays pipeable)."""
    cand = dict(GOLDEN_CANDIDATES["quad_runner"])
    cand["status"] = "candidate"  # not yet rendered -> note, not failure
    store.add_candidate(cand, db_path=db)
    pm.main(["--db", db, "--candidate", "golden-quad-runner"])
    captured = capsys.readouterr()
    meta = json.loads(captured.out)
    assert meta["title"] == ("QUAD RUNNER OF THE WEEK — Week 24 — "
                             "xerial (dm3)")
    assert "status 'candidate'" in captured.err


def test_cli_unknown_candidate_exits_nonzero(db):
    with pytest.raises(SystemExit):
        pm.main(["--db", db, "--candidate", "nope"])


# --------------------------------------------------------- golden regen tool

def regenerate_golden_files():
    """Rewrite tests/golden/publish_metadata/*.metadata.json from the
    fixtures above. Run after an INTENDED template change, review the diff."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for name, cand in GOLDEN_CANDIDATES.items():
        path = pm.write_metadata(pm.generate(cand),
                                 GOLDEN_DIR / f"{name}.metadata.json")
        print(f"regenerated: {path}")


if __name__ == "__main__":
    import sys
    if "--regen" in sys.argv:
        regenerate_golden_files()
    else:
        sys.exit("usage: python -m tests.test_publish_metadata --regen")
