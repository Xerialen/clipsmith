"""Tests for clipsmith.digest — week boundaries, ranking/cap, empty-category
note, schema shape, mark-posted lifecycle, CLI smoke. Temp DBs only, no
network."""
import json
import datetime as dt
from pathlib import Path

import pytest

from clipsmith import digest, store

WEEK = "2026-W24"               # Mon 2026-06-08 .. Sun 2026-06-14
WEEK_MON = "2026-06-08"
WEEK_SUN = "2026-06-14"


def make_candidate(cand_id, category="QUAD RUNNER", matchdate=WEEK_MON,
                   score=5.0, hub_url="https://hub.quakeworld.nu/games"
                   "?gameId=1&from=10&track=42",
                   detected_at="2026-06-10T18:30:00+00:00", status="candidate"):
    return {
        "id": cand_id,
        "category": category,
        "detected_at": detected_at,
        "demo_sha": "ab" * 32,
        "map": "dm3",
        "matchdate": matchdate,
        "teams": [{"name": "tot", "score": 187}, {"name": "-s-", "score": 142}],
        "pov_player": {"name": "xerial", "userid": 42},
        "window": {"start_s": 312.4, "end_s": 347.9},
        "score": score,
        "metrics": {
            "frags_in_run": 4,
            "kills": [{"t_s": 319.2, "weapon": "rl", "victim": "bps",
                       "teamkill": False}],
        },
        "description": f"{cand_id}: described moment.",
        "hub_url": hub_url,
        "status": status,
    }


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "candidates.db")


def section(dig, category):
    return next(s for s in dig["sections"] if s["category"] == category)


# ------------------------------------------------------------ week math

def test_parse_week():
    start, end = digest.parse_week(WEEK)
    assert start == dt.date(2026, 6, 8)
    assert end == dt.date(2026, 6, 14)


@pytest.mark.parametrize("bad", ["2026-24", "W24", "2026-W00", "2026-W60", ""])
def test_parse_week_rejects_garbage(bad):
    with pytest.raises(ValueError):
        digest.parse_week(bad)


@pytest.mark.parametrize("today,expected", [
    (dt.date(2026, 6, 10), "2026-W24"),   # Wednesday mid-week -> current week
    (dt.date(2026, 6, 14), "2026-W24"),   # Sunday -> still current week
    (dt.date(2026, 6, 15), "2026-W24"),   # the digest Monday -> week just ended
    (dt.date(2026, 6, 16), "2026-W25"),   # Tuesday -> new week underway
])
def test_default_week(today, expected):
    assert digest.default_week(today) == expected


def test_candidate_week_date_matchdate_governs():
    cand = make_candidate("c1", matchdate="2026-06-09",
                          detected_at="2026-06-20T00:00:00+00:00")
    assert digest.candidate_week_date(cand) == dt.date(2026, 6, 9)


def test_candidate_week_date_detected_at_fallback():
    cand = make_candidate("c1", matchdate="",
                          detected_at="2026-06-09T23:59:00+00:00")
    assert digest.candidate_week_date(cand) == dt.date(2026, 6, 9)


# --------------------------------------------------- selection boundaries

def test_week_boundary_selection(db):
    store.add_candidate(make_candidate("on-monday", matchdate=WEEK_MON), db_path=db)
    store.add_candidate(make_candidate("on-sunday", matchdate=WEEK_SUN), db_path=db)
    store.add_candidate(make_candidate("before", matchdate="2026-06-07"), db_path=db)
    store.add_candidate(make_candidate("after", matchdate="2026-06-15"), db_path=db)
    dig = digest.build_digest(WEEK, db_path=db)
    ids = {c["candidate_id"] for c in section(dig, "QUAD RUNNER")["candidates"]}
    assert ids == {"on-monday", "on-sunday"}


def test_only_status_candidate_selected(db):
    store.add_candidate(make_candidate("live"), db_path=db)
    store.add_candidate(make_candidate("gone", status="rejected"), db_path=db)
    store.add_candidate(make_candidate("done", status="posted"), db_path=db)
    dig = digest.build_digest(WEEK, db_path=db)
    ids = [c["candidate_id"] for c in section(dig, "QUAD RUNNER")["candidates"]]
    assert ids == ["live"]


def test_empty_matchdate_uses_detected_at(db):
    store.add_candidate(make_candidate(
        "undated", matchdate="", detected_at="2026-06-10T12:00:00+00:00"),
        db_path=db)
    dig = digest.build_digest(WEEK, db_path=db)
    ids = [c["candidate_id"] for c in section(dig, "QUAD RUNNER")["candidates"]]
    assert ids == ["undated"]


# ------------------------------------------------------- ranking and cap

def test_top3_cap_and_ranking(db):
    for cid, score in (("a", 1.0), ("b", 9.0), ("c", 5.0), ("d", 7.0)):
        store.add_candidate(make_candidate(cid, score=score), db_path=db)
    sec = section(digest.build_digest(WEEK, db_path=db), "QUAD RUNNER")
    assert [c["candidate_id"] for c in sec["candidates"]] == ["b", "d", "c"]
    assert [c["rank_in_category"] for c in sec["candidates"]] == [1, 2, 3]
    assert sec["note"] is None


def test_ranking_tie_stable_by_id(db):
    for cid in ("z-last", "a-first", "m-mid"):
        store.add_candidate(make_candidate(cid, score=5.0), db_path=db)
    ids1 = [c["candidate_id"] for c in
            section(digest.build_digest(WEEK, db_path=db),
                    "QUAD RUNNER")["candidates"]]
    ids2 = [c["candidate_id"] for c in
            section(digest.build_digest(WEEK, db_path=db),
                    "QUAD RUNNER")["candidates"]]
    assert ids1 == ["a-first", "m-mid", "z-last"]
    assert ids1 == ids2


def test_rebuild_deterministic_except_timestamp(db):
    for cid, score in (("a", 1.0), ("b", 9.0)):
        store.add_candidate(make_candidate(cid, score=score), db_path=db)
    now = dt.datetime(2026, 6, 15, 20, 0, tzinfo=dt.timezone.utc)
    d1 = digest.build_digest(WEEK, db_path=db, now=now)
    d2 = digest.build_digest(WEEK, db_path=db, now=now)
    assert d1 == d2


# ------------------------------------------------- empty-category marker

def test_zero_category_gets_explicit_note(db):
    store.add_candidate(make_candidate("q1", category="QUAD RUNNER"), db_path=db)
    dig = digest.build_digest(WEEK, db_path=db)
    assert [s["category"] for s in dig["sections"]] == list(digest.CATEGORIES)
    pent = section(dig, "PENT GRIND")
    assert pent["candidates"] == []
    assert pent["note"] == "no qualifying PENT GRIND moment this week"
    assert section(dig, "QUAD RUNNER")["note"] is None


# --------------------------------------- COMEBACK fifth category (issue #30)

class TestComebackFlag:
    """COMEBACK is digest-wired behind a DEFAULT-OFF flag: off -> the
    document is byte-identical to the 4-section Phase-2 contract; on -> a
    fifth section with the same min-1/max-3 + empty-note rules."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv(digest.COMEBACK_ENV_VAR, raising=False)

    def test_default_off_is_byte_identical_without_comeback_candidates(
            self, tmp_path):
        db4 = str(tmp_path / "four.db")
        db5 = str(tmp_path / "five.db")
        for db_ in (db4, db5):
            store.add_candidate(make_candidate("q1"), db_path=db_)
        # the COMEBACK candidate exists ONLY in db5 — with the flag off it
        # must not influence a single byte of the output
        store.add_candidate(make_candidate("cb1", category="COMEBACK"),
                            db_path=db5)
        now = dt.datetime(2026, 6, 15, 20, 0, tzinfo=dt.timezone.utc)
        d4 = digest.build_digest(WEEK, db_path=db4, now=now)
        d5 = digest.build_digest(WEEK, db_path=db5, now=now)
        assert json.dumps(d4, indent=2) == json.dumps(d5, indent=2)
        assert [s["category"] for s in d5["sections"]] == \
            list(digest.CATEGORIES)

    def test_flag_on_appends_fifth_section(self, db):
        store.add_candidate(make_candidate("cb1", category="COMEBACK"),
                            db_path=db)
        dig = digest.build_digest(WEEK, db_path=db, include_comeback=True)
        assert [s["category"] for s in dig["sections"]] == \
            list(digest.CATEGORIES) + ["COMEBACK"]
        sec = section(dig, "COMEBACK")
        assert sec["label"] == "COMEBACK OF THE WEEK — candidates"
        assert sec["note"] is None
        assert [c["candidate_id"] for c in sec["candidates"]] == ["cb1"]

    def test_flag_on_cap_and_empty_note_rules(self, db):
        # max 3 by score
        for cid, score in (("a", 1.0), ("b", 9.0), ("c", 5.0), ("d", 7.0)):
            store.add_candidate(
                make_candidate(cid, category="COMEBACK", score=score),
                db_path=db)
        dig = digest.build_digest(WEEK, db_path=db, include_comeback=True)
        sec = section(dig, "COMEBACK")
        assert [c["candidate_id"] for c in sec["candidates"]] == ["b", "d", "c"]
        assert [c["rank_in_category"] for c in sec["candidates"]] == [1, 2, 3]
        # empty section -> explicit machine-distinguishable note
        empty = digest.build_digest("2031-W01", db_path=db,
                                    include_comeback=True)
        sec = section(empty, "COMEBACK")
        assert sec["candidates"] == []
        assert sec["note"] == "no qualifying COMEBACK moment this week"

    def test_env_var_enables_and_explicit_flag_wins(self, db, monkeypatch):
        store.add_candidate(make_candidate("cb1", category="COMEBACK"),
                            db_path=db)
        monkeypatch.setenv(digest.COMEBACK_ENV_VAR, "1")
        dig = digest.build_digest(WEEK, db_path=db)
        assert [s["category"] for s in dig["sections"]][-1] == "COMEBACK"
        # an explicit False overrides the env var
        dig = digest.build_digest(WEEK, db_path=db, include_comeback=False)
        assert "COMEBACK" not in [s["category"] for s in dig["sections"]]
        # non-truthy env values stay off
        monkeypatch.setenv(digest.COMEBACK_ENV_VAR, "0")
        dig = digest.build_digest(WEEK, db_path=db)
        assert "COMEBACK" not in [s["category"] for s in dig["sections"]]

    def test_cli_include_comeback_flag(self, db, tmp_path, capsys):
        store.add_candidate(make_candidate("cb1", category="COMEBACK"),
                            db_path=db)
        out = tmp_path / f"{WEEK}.json"
        digest.main(["build", "--db", db, "--week", WEEK, "--out", str(out),
                     "--include-comeback"])
        echoed = json.loads(capsys.readouterr().out)
        assert [s["category"] for s in echoed["sections"]][-1] == "COMEBACK"
        ids = [c["candidate_id"]
               for s in echoed["sections"] for c in s["candidates"]]
        assert ids == ["cb1"]


# ------------------------------------------------------------ schema shape

def test_digest_schema_shape(db):
    store.add_candidate(make_candidate("q1"), db_path=db)
    store.add_candidate(make_candidate("t1", category="TEAM KILLER",
                                       hub_url=""), db_path=db)
    dig = digest.build_digest(WEEK, db_path=db)

    assert set(dig) == {"week", "week_start", "week_end", "generated_at",
                        "sections"}
    assert dig["week"] == WEEK
    assert dig["week_start"] == WEEK_MON
    assert dig["week_end"] == WEEK_SUN
    dt.datetime.fromisoformat(dig["generated_at"])  # parseable timestamp

    for sec in dig["sections"]:
        assert set(sec) == {"category", "label", "note", "candidates"}
        assert sec["label"] == f"{sec['category']} OF THE WEEK — candidates"

    entry = section(dig, "QUAD RUNNER")["candidates"][0]
    assert set(entry) == {
        "candidate_id", "rank_in_category", "score", "pov_player", "map",
        "matchdate", "teams", "window", "description", "hub_url",
        "hub_url_missing", "metrics_summary",
    }
    assert entry["hub_url_missing"] is False
    # metrics_summary: scalars kept, lists collapsed to counts
    assert entry["metrics_summary"] == {"frags_in_run": 4, "kills_count": 1}

    missing = section(dig, "TEAM KILLER")["candidates"][0]
    assert missing["hub_url"] == ""
    assert missing["hub_url_missing"] is True

    json.dumps(dig)  # JSON-serializable end to end


# ------------------------------------------------------------- mark-posted

def test_mark_posted_flips_and_is_idempotent(db, tmp_path):
    store.add_candidate(make_candidate("q1"), db_path=db)
    store.add_candidate(make_candidate("t1", category="TEAM KILLER"), db_path=db)
    out = tmp_path / "digest.json"
    digest.write_digest(digest.build_digest(WEEK, db_path=db), out)

    results = digest.mark_posted(WEEK, db_path=db, digest_path=str(out))
    assert dict(results) == {"q1": "posted", "t1": "posted"}
    assert store.get_candidate("q1", db_path=db)["status"] == "posted"
    assert store.get_candidate("t1", db_path=db)["status"] == "posted"

    # second run: clean per-candidate skip, no crash, statuses unchanged
    rerun = digest.mark_posted(WEEK, db_path=db, digest_path=str(out))
    assert dict(rerun) == {"q1": "skipped (status=posted)",
                           "t1": "skipped (status=posted)"}
    assert store.get_candidate("q1", db_path=db)["status"] == "posted"


def test_mark_posted_without_digest_errors(db):
    with pytest.raises(ValueError, match="run build first"):
        digest.mark_posted("2031-W01", db_path=db,
                           digest_path="does/not/exist.json")


# -------------------------------------------------------------- CLI smoke

def test_cli_build_writes_and_echoes(db, tmp_path, capsys):
    store.add_candidate(make_candidate("q1"), db_path=db)
    out = tmp_path / "digests" / f"{WEEK}.json"
    digest.main(["build", "--db", db, "--week", WEEK, "--out", str(out)])

    echoed = json.loads(capsys.readouterr().out)
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert echoed == on_disk
    assert echoed["week"] == WEEK
    ids = [c["candidate_id"]
           for s in echoed["sections"] for c in s["candidates"]]
    assert ids == ["q1"]


def test_cli_mark_posted(db, tmp_path, capsys):
    store.add_candidate(make_candidate("q1"), db_path=db)
    out = tmp_path / f"{WEEK}.json"
    digest.write_digest(digest.build_digest(WEEK, db_path=db), out)
    digest.main(["mark-posted", "--week", WEEK, "--db", db,
                 "--digest", str(out)])
    assert "q1: posted" in capsys.readouterr().out
    assert store.get_candidate("q1", db_path=db)["status"] == "posted"


def test_cli_rejects_bad_week(db, capsys):
    with pytest.raises(SystemExit):
        digest.main(["build", "--db", db, "--week", "junk"])
