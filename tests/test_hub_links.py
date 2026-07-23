"""Tests for clipsmith.hub_links — URL formatting, from/track derivation,
miss handling, store fill mode. Zero network: the http_get layer is faked.

Also covers store.set_hub_url (added for issue #9) since test_store.py is
owned by another ticket.
"""
import json

import pytest

from clipsmith import hub_links, store

KIPPO_SHA = "eb7f447ac8f709b2b9d0a345035926835bf314b5b43b052f2ca6ee16b4ee9081"


def fake_http_get(rows, capture=None):
    """An http_get double returning the given PostgREST rows as JSON."""
    def _get(url, headers):
        if capture is not None:
            capture.append((url, headers))
        return json.dumps(rows).encode()
    return _get


def failing_http_get(url, headers):
    raise hub_links.HubLookupError("hub lookup: HTTP 500")


# ------------------------------------------------------------- URL building

def test_build_hub_url_format():
    url = hub_links.build_hub_url(221227, 726.0, 58)
    assert url == "https://hub.quakeworld.nu/games/?gameId=221227&from=726&track=58"


def test_build_hub_url_applies_demo_offset():
    # hub `from` is demo-relative: match-relative + countdown (verified
    # 2026-06-10: hub Share dialog showed from=665 at match clock 10:55)
    url = hub_links.build_hub_url(221227, 726.0, 58, demo_offset_s=10.0)
    assert "from=736" in url


def test_from_is_floored():
    assert hub_links.derive_from(726.9) == 726
    assert hub_links.derive_from(726.5, demo_offset_s=10.0) == 736
    assert hub_links.build_hub_url(1, 311.7, 42).endswith("from=311&track=42")


def test_from_clamped_to_zero():
    assert hub_links.derive_from(-3.2) == 0
    assert hub_links.derive_from(0) == 0


def test_demo_time_offset_from_overview():
    got = hub_links.demo_time_offset(
        KIPPO_SHA, http_get=fake_http_get({"matchStart": 10000}))
    assert got == 10.0


def test_demo_time_offset_falls_back_to_ktx_countdown():
    assert hub_links.demo_time_offset(
        KIPPO_SHA, http_get=failing_http_get) == hub_links.DEFAULT_COUNTDOWN_S
    # non-dict body (e.g. a PostgREST list) must also fall back, not crash
    assert hub_links.demo_time_offset(
        KIPPO_SHA, http_get=fake_http_get([{"id": 1}])
    ) == hub_links.DEFAULT_COUNTDOWN_S


def test_track_is_userid():
    assert "track=58" in hub_links.build_hub_url(9, 0, 58)


# --------------------------------------------------------------- resolution

def test_resolve_game_id_hit():
    captured = []
    got = hub_links.resolve_game_id(
        KIPPO_SHA, http_get=fake_http_get([{"id": 221063}], captured)
    )
    assert got == 221063
    url, headers = captured[0]
    assert f"demo_sha256=eq.{KIPPO_SHA}" in url
    assert "select=id" in url
    assert headers["apikey"] == headers["Authorization"].removeprefix("Bearer ")


def test_resolve_game_id_normalizes_sha():
    captured = []
    hub_links.resolve_game_id(
        "  " + KIPPO_SHA.upper() + " ", http_get=fake_http_get([{"id": 1}], captured)
    )
    assert f"demo_sha256=eq.{KIPPO_SHA}" in captured[0][0]


def test_resolve_game_id_miss_returns_none():
    assert hub_links.resolve_game_id("ab" * 32, http_get=fake_http_get([])) is None


def test_resolve_game_id_empty_sha_raises():
    with pytest.raises(ValueError):
        hub_links.resolve_game_id("   ", http_get=fake_http_get([]))


def test_resolve_game_id_bad_json_raises():
    def garbage(url, headers):
        return b"<html>not json</html>"
    with pytest.raises(hub_links.HubLookupError):
        hub_links.resolve_game_id(KIPPO_SHA, http_get=garbage)


def test_resolve_hub_url_hit_and_miss():
    # explicit demo_offset_s keeps the test off the network (None would
    # ask mvd-api for matchStart)
    hit = hub_links.resolve_hub_url(
        KIPPO_SHA, 726.0, 58, http_get=fake_http_get([{"id": 221063}]),
        demo_offset_s=10.0,
    )
    assert hit == "https://hub.quakeworld.nu/games/?gameId=221063&from=736&track=58"
    miss = hub_links.resolve_hub_url("cd" * 32, 10, 5, http_get=fake_http_get([]),
                                     demo_offset_s=0.0)
    assert miss == ""


def test_network_error_is_not_a_miss():
    with pytest.raises(hub_links.HubLookupError):
        hub_links.resolve_hub_url(KIPPO_SHA, 0, 1, http_get=failing_http_get)


# ------------------------------------------------------------- store fill

@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "candidates.db")


def make_candidate(db, hub_url=""):
    cand = {
        "category": "QUAD RUNNER",
        "demo_sha": KIPPO_SHA,
        "map": "dm3",
        "matchdate": "2026-06-08",
        "teams": [{"name": "tot", "score": 187}, {"name": "-s-", "score": 142}],
        "pov_player": {"name": "kippo", "userid": 58},
        "window": {"start_s": 726.4, "end_s": 759.0},
        "score": 8.5,
        "metrics": {"frags_in_window": 4},
        "description": "kippo's quad run on dm3.",
        "hub_url": hub_url,
    }
    return store.add_candidate(cand, db_path=db)


def test_set_hub_url(db):
    cid = make_candidate(db)
    store.set_hub_url(cid, "https://hub.quakeworld.nu/games?gameId=1&from=0&track=1",
                      db_path=db)
    cand = store.get_candidate(cid, db_path=db)
    assert cand["hub_url"].endswith("gameId=1&from=0&track=1")
    assert cand["status"] == "candidate"  # untouched


def test_set_hub_url_unknown_id(db):
    with pytest.raises(ValueError):
        store.set_hub_url("nope", "x", db_path=db)


def test_fill_candidate_hit(db):
    cid = make_candidate(db)
    url = hub_links.fill_candidate(
        cid, db_path=db, http_get=fake_http_get([{"id": 221063}]),
        demo_offset_s=10.0,
    )
    assert url == "https://hub.quakeworld.nu/games/?gameId=221063&from=736&track=58"
    assert store.get_candidate(cid, db_path=db)["hub_url"] == url


def test_fill_candidate_miss_keeps_empty_hub_url(db):
    cid = make_candidate(db)
    url = hub_links.fill_candidate(cid, db_path=db, http_get=fake_http_get([]),
                                   demo_offset_s=0.0)
    assert url == ""
    # candidate keeps hub_url "" — digest builder (P1.9) must render a
    # "no hub link available" note and the resolver can retry later
    assert store.get_candidate(cid, db_path=db)["hub_url"] == ""


# ----------------------------------------------------------------- CLI

def test_cli_sha_mode_hit(monkeypatch, capsys):
    monkeypatch.setattr(
        hub_links, "_default_http_get", fake_http_get([{"id": 221227}])
    )
    hub_links.main(["--sha", "8d" * 32, "--from", "100.7", "--track", "7",
                    "--offset", "0"])
    out = capsys.readouterr().out.strip()
    assert out == "https://hub.quakeworld.nu/games/?gameId=221227&from=100&track=7"


def test_cli_sha_mode_default_offset_falls_back_to_countdown(monkeypatch, capsys):
    # the monkeypatched http_get returns a list for the overview probe too,
    # so the offset falls back to the 10 s KTX countdown
    monkeypatch.setattr(
        hub_links, "_default_http_get", fake_http_get([{"id": 221227}])
    )
    hub_links.main(["--sha", "8d" * 32, "--from", "100", "--track", "7"])
    assert "from=110" in capsys.readouterr().out


def test_cli_sha_mode_miss_exits_not_found(monkeypatch, capsys):
    monkeypatch.setattr(hub_links, "_default_http_get", fake_http_get([]))
    with pytest.raises(SystemExit) as exc:
        hub_links.main(["--sha", "ab" * 32, "--from", "0", "--track", "1"])
    assert exc.value.code == hub_links.EXIT_NOT_FOUND
    assert "not on hub yet" in capsys.readouterr().err


def test_cli_candidate_mode_fills_store(monkeypatch, capsys, db):
    cid = make_candidate(db)
    monkeypatch.setattr(
        hub_links, "_default_http_get", fake_http_get([{"id": 221063}])
    )
    hub_links.main(["--candidate", cid, "--db", db])
    out = capsys.readouterr().out.strip()
    assert out == store.get_candidate(cid, db_path=db)["hub_url"]
    assert "gameId=221063" in out


def test_cli_candidate_mode_miss_exits_not_found(monkeypatch, db):
    cid = make_candidate(db)
    monkeypatch.setattr(hub_links, "_default_http_get", fake_http_get([]))
    with pytest.raises(SystemExit) as exc:
        hub_links.main(["--candidate", cid, "--db", db])
    assert exc.value.code == hub_links.EXIT_NOT_FOUND
    assert store.get_candidate(cid, db_path=db)["hub_url"] == ""


def test_cli_lookup_error_exits_nonzero(monkeypatch):
    monkeypatch.setattr(hub_links, "_default_http_get", failing_http_get)
    with pytest.raises(SystemExit) as exc:
        hub_links.main(["--sha", "ab" * 32, "--from", "0", "--track", "1"])
    assert exc.value.code != 0
    assert exc.value.code != hub_links.EXIT_NOT_FOUND


def test_cli_sha_requires_from_and_track():
    with pytest.raises(SystemExit) as exc:
        hub_links.main(["--sha", "ab" * 32])
    assert exc.value.code == 2  # argparse usage error
