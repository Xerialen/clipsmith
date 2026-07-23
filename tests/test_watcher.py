"""Tests for clipsmith.watcher — watermark semantics + env-file parser.

All network access is mocked/injected; no live credentials are used.
"""
import json

import pytest

from clipsmith import watcher


# ------------------------------------------------------------------ fixtures

def make_game(rowid, sha=None):
    return {
        "rowid": rowid,
        "sha": sha or f"{rowid:064x}",
        "map": "dm3",
        "matchdate": "2026-06-09 20:00:00 +0000",
        "teams": [{"name": "blue", "score": 100}, {"name": "red", "score": 90}],
    }


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "state"


def run(state_dir, games, fail_rowids=(), crash_rowids=(), no_download=False,
        limit=500, dry_run=False):
    """Drive run_watch with injected query/download fns; returns the record.

    ``fail_rowids`` raise a caught exception (per-game failure);
    ``crash_rowids`` raise KeyboardInterrupt — uncaught by the per-game
    handler, simulating the process dying mid-batch.
    """
    downloaded = []

    def query_fn(wm, lim):
        return [g for g in games if g["rowid"] > wm][:lim]

    def download_fn(game, demos_dir):
        if game["rowid"] in crash_rowids:
            raise KeyboardInterrupt("simulated process death")
        if game["rowid"] in fail_rowids:
            raise RuntimeError("boom")
        downloaded.append(game["rowid"])

    record = watcher.run_watch(
        state_dir=state_dir, no_download=no_download, limit=limit,
        query_fn=query_fn, max_rowid_fn=lambda: max(
            (g["rowid"] for g in games), default=0),
        download_fn=download_fn, out=open(state_dir.parent / "out.txt", "a"),
        dry_run=dry_run,
    )
    return record, downloaded


def _snapshot(state_dir):
    """Byte-exact snapshot of every file under state_dir (dry-run proof)."""
    if not state_dir.exists():
        return {}
    return {p.relative_to(state_dir).as_posix(): p.read_bytes()
            for p in sorted(state_dir.rglob("*")) if p.is_file()}


# ------------------------------------------------------------ env-file parser

def test_load_creds_parses_key_values(tmp_path):
    env = tmp_path / "keys.env"
    env.write_text(
        "# comment line\n"
        "OTHER_KEY=ignored\n"
        'TURSO_DB_URL="libsql://example-db.turso.io"\n'
        "TURSO_AUTH_TOKEN_QWSTATS='tok123'\n"
        "not a kv line\n",
        encoding="utf-8",
    )
    url, token = watcher.load_creds(env)
    assert url == "https://example-db.turso.io"  # libsql:// converted, quotes stripped
    assert token == "tok123"


def test_load_creds_plain_https_url_untouched(tmp_path):
    env = tmp_path / "keys.env"
    env.write_text(
        "TURSO_DB_URL=https://example-db.turso.io/\n"
        "TURSO_AUTH_TOKEN_QWSTATS=tok\n",
        encoding="utf-8",
    )
    url, _ = watcher.load_creds(env)
    assert url == "https://example-db.turso.io"  # trailing slash stripped


def test_load_creds_missing_keys_raises(tmp_path):
    env = tmp_path / "keys.env"
    env.write_text("TURSO_DB_URL=https://x\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        watcher.load_creds(env)
    assert "TURSO_AUTH_TOKEN_QWSTATS" in str(exc.value)


def test_load_creds_error_never_contains_values(tmp_path):
    env = tmp_path / "keys.env"
    env.write_text("TURSO_DB_URL=https://secret-host.turso.io\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        watcher.load_creds(env)
    assert "secret-host" not in str(exc.value)


# -------------------------------------------------------- watermark mechanics

def test_cold_start_initializes_to_head_without_processing(state_dir):
    games = [make_game(10), make_game(11)]
    record, downloaded = run(state_dir, games)
    assert record["event"] == "cold_start"
    assert record["watermark_after"] == 11
    assert downloaded == []  # nothing processed on cold start
    assert watcher.read_watermark(state_dir) == 11


def test_full_success_advances_watermark_to_last_rowid(state_dir):
    watcher.write_watermark(state_dir, 5)
    games = [make_game(6), make_game(7), make_game(8)]
    record, downloaded = run(state_dir, games)
    assert downloaded == [6, 7, 8]
    assert record["watermark_before"] == 5
    assert record["watermark_after"] == 8
    assert watcher.read_watermark(state_dir) == 8


def test_no_new_games_is_clean_noop(state_dir):
    watcher.write_watermark(state_dir, 8)
    record, downloaded = run(state_dir, [make_game(6)])
    assert record["found"] == 0
    assert downloaded == []
    assert record["watermark_after"] == 8
    assert watcher.read_watermark(state_dir) == 8


def test_failure_freezes_watermark_before_first_failed_game(state_dir):
    watcher.write_watermark(state_dir, 5)
    games = [make_game(6), make_game(7), make_game(8)]
    record, downloaded = run(state_dir, games, fail_rowids={7})
    # 6 succeeded, 7 failed, 8 still attempted but must not advance watermark
    assert downloaded == [6, 8]
    assert record["watermark_after"] == 6
    assert watcher.read_watermark(state_dir) == 6
    assert [f["rowid"] for f in record["failures"]] == [7]


def _pending_shas(state_dir):
    path = state_dir / "pending-detection.jsonl"
    if not path.exists():
        return []
    import json
    with open(path, encoding="utf-8") as f:
        return [json.loads(line)["sha"] for line in f if line.strip()]


def test_success_appends_durable_pending_detection_handoff(state_dir):
    # Watermark = "fetched"; the detection stage consumes this file,
    # so an advanced watermark can never strand an undetected demo.
    watcher.write_watermark(state_dir, 5)
    games = [make_game(6), make_game(7)]
    run(state_dir, games)
    assert _pending_shas(state_dir) == [games[0]["sha"], games[1]["sha"]]


def test_failed_game_not_added_to_pending_detection(state_dir):
    watcher.write_watermark(state_dir, 5)
    games = [make_game(6), make_game(7)]
    run(state_dir, games, fail_rowids={7})
    assert _pending_shas(state_dir) == [games[0]["sha"]]


def test_pending_detection_dedupes_on_replay(state_dir):
    watcher.write_watermark(state_dir, 5)
    games = [make_game(6)]
    run(state_dir, games)
    # rewind via the explicit reset path (raw regression writes are refused)
    watcher.reset_watermark(state_dir, 5, out=open(state_dir / "out.txt", "a"))
    run(state_dir, games)  # replay must not double-queue
    assert _pending_shas(state_dir) == [games[0]["sha"]]


def test_failure_on_first_game_does_not_advance_at_all(state_dir):
    watcher.write_watermark(state_dir, 5)
    record, _ = run(state_dir, [make_game(6), make_game(7)], fail_rowids={6})
    assert record["watermark_after"] == 5
    assert watcher.read_watermark(state_dir) == 5


def test_retry_after_failure_picks_up_unprocessed_games(state_dir):
    watcher.write_watermark(state_dir, 5)
    games = [make_game(6), make_game(7), make_game(8)]
    run(state_dir, games, fail_rowids={7})
    assert watcher.read_watermark(state_dir) == 6
    # next run, failure resolved: 7 and 8 are re-seen and watermark catches up
    record, downloaded = run(state_dir, games)
    assert downloaded == [7, 8]
    assert record["watermark_after"] == 8


def test_reset_watermark_rewinds_and_replays(state_dir):
    watcher.write_watermark(state_dir, 8)
    watcher.reset_watermark(state_dir, 5, out=open(state_dir / "out.txt", "a"))
    assert watcher.read_watermark(state_dir) == 5
    record, downloaded = run(state_dir, [make_game(6), make_game(7)])
    assert downloaded == [6, 7]
    assert record["watermark_after"] == 7


# --------------------------- issue #32: crash recovery + write-ahead marker

def test_crash_mid_run_leaves_watermark_at_pre_run_value(state_dir):
    """A crash (uncaught exception / process death) must NOT advance the
    watermark — not even past the successfully-processed prefix."""
    watcher.write_watermark(state_dir, 5)
    games = [make_game(6), make_game(7), make_game(8)]
    with pytest.raises(KeyboardInterrupt):
        run(state_dir, games, crash_rowids={7})
    assert watcher.read_watermark(state_dir) == 5


def test_crash_leaves_write_ahead_marker_with_scan_target(state_dir):
    watcher.write_watermark(state_dir, 5)
    games = [make_game(6), make_game(7), make_game(8)]
    with pytest.raises(KeyboardInterrupt):
        run(state_dir, games, crash_rowids={7})
    marker = watcher.read_inprogress(state_dir)
    assert marker is not None
    assert marker["watermark_before"] == 5
    assert marker["batch_first_rowid"] == 6
    assert marker["batch_last_rowid"] == 8
    assert marker["found"] == 3


def test_crash_then_rerun_converges_to_uninterrupted_state(state_dir, tmp_path):
    """Issue #32 Measurement: crash mid-batch + rerun must equal one
    uninterrupted run — same watermark, same pending-detection contents."""
    games = [make_game(6), make_game(7), make_game(8)]

    # Reference: uninterrupted run in a separate state dir.
    ref_dir = tmp_path / "ref-state"
    watcher.write_watermark(ref_dir, 5)
    run(ref_dir, games)

    # Crash after game 6 was processed (game 7 dies mid-flight) ...
    watcher.write_watermark(state_dir, 5)
    with pytest.raises(KeyboardInterrupt):
        run(state_dir, games, crash_rowids={7})
    assert watcher.read_watermark(state_dir) == 5
    assert _pending_shas(state_dir) == [games[0]["sha"]]  # partial emission

    # ... then a plain rerun to completion.
    record, downloaded = run(state_dir, games)

    assert record["recovered_inprogress"] is not None  # crash was detected
    assert watcher.read_watermark(state_dir) == watcher.read_watermark(ref_dir) == 8
    assert _pending_shas(state_dir) == _pending_shas(ref_dir)
    assert watcher.read_inprogress(state_dir) is None  # marker cleaned up


def test_crash_after_watermark_before_marker_clear_is_stale_marker(
        state_dir, monkeypatch):
    """A crash in the window AFTER write_watermark but BEFORE
    clear_inprogress leaves a marker whose batch already completed. The next
    run must NOT claim the batch "will be re-processed" (the advanced
    watermark means it won't be) — it detects the stale marker, clears it,
    and reports the batch as completed."""
    watcher.write_watermark(state_dir, 5)
    games = [make_game(6), make_game(7)]

    def dying_clear(sd):
        raise KeyboardInterrupt("died after watermark write, before clear")

    monkeypatch.setattr(watcher, "clear_inprogress", dying_clear)
    with pytest.raises(KeyboardInterrupt):
        run(state_dir, games)
    monkeypatch.undo()

    # The crash window: watermark DID advance, marker was left behind.
    assert watcher.read_watermark(state_dir) == 7
    assert watcher.read_inprogress(state_dir) is not None

    record, downloaded = run(state_dir, games)
    assert downloaded == []                              # NOT re-processed
    assert record["recovered_inprogress"]["stale"] is True
    assert record["recovered_inprogress"]["batch_last_rowid"] == 7
    assert watcher.read_inprogress(state_dir) is None    # stale marker cleared
    assert watcher.read_watermark(state_dir) == 7


def test_dry_run_reports_stale_marker_but_leaves_it_in_place(
        state_dir, monkeypatch):
    """Dry runs stay inert even for stale markers: reported, not consumed."""
    watcher.write_watermark(state_dir, 5)
    monkeypatch.setattr(watcher, "clear_inprogress",
                        lambda sd: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run(state_dir, [make_game(6)])
    monkeypatch.undo()
    before = _snapshot(state_dir)
    record, _ = run(state_dir, [make_game(6)], dry_run=True)
    assert record["recovered_inprogress"]["stale"] is True
    assert _snapshot(state_dir) == before  # marker untouched, nothing written


def test_clean_run_clears_marker_and_reports_no_recovery(state_dir):
    watcher.write_watermark(state_dir, 5)
    record, _ = run(state_dir, [make_game(6)])
    assert record["recovered_inprogress"] is None
    assert watcher.read_inprogress(state_dir) is None


def test_partial_failure_still_clears_marker(state_dir):
    """Caught per-game failures are not crashes: they are recorded in the
    run log and retried via the frozen watermark, so the marker comes off."""
    watcher.write_watermark(state_dir, 5)
    record, _ = run(state_dir, [make_game(6), make_game(7)], fail_rowids={7})
    assert record["failures"]
    assert watcher.read_inprogress(state_dir) is None
    assert watcher.read_watermark(state_dir) == 6


# ----------------------- issue #32: watermark non-regression + corrupt file

def test_write_watermark_refuses_regression(state_dir):
    watcher.write_watermark(state_dir, 8)
    with pytest.raises(RuntimeError) as exc:
        watcher.write_watermark(state_dir, 5)
    assert "regression" in str(exc.value)
    assert watcher.read_watermark(state_dir) == 8  # untouched


def test_write_watermark_same_value_and_advance_are_fine(state_dir):
    watcher.write_watermark(state_dir, 8)
    watcher.write_watermark(state_dir, 8)   # no-op rewrite allowed
    watcher.write_watermark(state_dir, 9)   # forward always allowed
    assert watcher.read_watermark(state_dir) == 9


def test_corrupt_watermark_is_error_not_cold_start(state_dir):
    """An unreadable watermark must never look like a cold start — cold
    start re-initializes to head and would silently skip the backlog."""
    state_dir.mkdir(parents=True)
    watcher.watermark_path(state_dir).write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        run(state_dir, [make_game(6)])
    assert "cold start" in str(exc.value)


def test_watermark_with_null_rowid_is_error(state_dir):
    state_dir.mkdir(parents=True)
    watcher.watermark_path(state_dir).write_text('{"rowid": null}',
                                                 encoding="utf-8")
    with pytest.raises(RuntimeError):
        watcher.read_watermark(state_dir)


def test_reset_watermark_recovers_corrupt_file(state_dir):
    state_dir.mkdir(parents=True)
    watcher.watermark_path(state_dir).write_text("garbage", encoding="utf-8")
    watcher.reset_watermark(state_dir, 7, out=open(state_dir / "out.txt", "a"))
    assert watcher.read_watermark(state_dir) == 7


# ------------------------------------------- issue #32: --dry-run is inert

def test_dry_run_is_byte_identical_side_effect_free(state_dir):
    watcher.write_watermark(state_dir, 5)
    before = _snapshot(state_dir)
    record, downloaded = run(state_dir, [make_game(6), make_game(7)],
                             dry_run=True)
    assert downloaded == []                      # download_fn never invoked
    assert record["dry_run"] is True
    assert record["found"] == 2
    assert record["watermark_after"] == 5
    assert _snapshot(state_dir) == before        # nothing written at all


def test_dry_run_cold_start_writes_nothing(state_dir):
    record, downloaded = run(state_dir, [make_game(10)], dry_run=True)
    assert record["event"] == "cold_start" and record["dry_run"] is True
    assert record["would_initialize_to"] == 10
    assert downloaded == []
    assert watcher.read_watermark(state_dir) is None
    assert _snapshot(state_dir) == {}


def test_dry_run_does_not_clear_crash_marker(state_dir):
    watcher.write_watermark(state_dir, 5)
    with pytest.raises(KeyboardInterrupt):
        run(state_dir, [make_game(6)], crash_rowids={6})
    before = _snapshot(state_dir)
    record, _ = run(state_dir, [make_game(6)], dry_run=True)
    assert record["recovered_inprogress"] is not None  # still reported
    assert _snapshot(state_dir) == before              # but not consumed


# ------------------------------------------ issue #32: queue.jsonl dedup

def test_queue_jsonl_dedupes_on_replay(state_dir):
    watcher.write_watermark(state_dir, 5)
    games = [make_game(6, sha="abc123")]

    def go():  # real queue_demo path (no injected download_fn)
        return watcher.run_watch(
            state_dir=state_dir, no_download=True,
            query_fn=lambda wm, lim: [g for g in games if g["rowid"] > wm][:lim],
            max_rowid_fn=lambda: 6,
            out=open(state_dir.parent / "out.txt", "a"),
        )

    go()
    watcher.reset_watermark(state_dir, 5, out=open(state_dir / "out.txt", "a"))
    go()  # replay must not double-queue
    lines = (state_dir / "demos" / "queue.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["sha"] == "abc123"


def test_queue_jsonl_tolerates_legacy_and_torn_lines(state_dir):
    """An existing queue.jsonl may contain lines without a "sha"
    key (hand edits, foreign producers) or torn non-JSON lines (crash
    mid-append). They are skipped — not deduped against, never a crash —
    while keyed legacy entries still dedup normally."""
    watcher.write_watermark(state_dir, 5)
    demos_dir = state_dir / "demos"
    demos_dir.mkdir(parents=True)
    legacy = ('{"url": "https://example/no-sha-key.mvd.gz"}\n'
              '{"sha": "aaa111"}\n'
              '{"queued_at": "2026-01-01T00:00:00+00:00", "sh\n')  # torn append
    (demos_dir / "queue.jsonl").write_text(legacy, encoding="utf-8")

    games = [make_game(6, sha="bbb222"), make_game(7, sha="aaa111")]
    record = watcher.run_watch(
        state_dir=state_dir, no_download=True,
        query_fn=lambda wm, lim: [g for g in games if g["rowid"] > wm][:lim],
        max_rowid_fn=lambda: 7,
        out=open(state_dir.parent / "out.txt", "a"),
    )
    assert record["failures"] == []
    assert record["watermark_after"] == 7
    text = (demos_dir / "queue.jsonl").read_text(encoding="utf-8")
    assert text.startswith(legacy)  # legacy lines preserved untouched
    appended = text[len(legacy):].strip().splitlines()
    # bbb222 appended once; aaa111 deduped against the keyed legacy entry
    assert [json.loads(l)["sha"] for l in appended] == ["bbb222"]


def test_pending_detection_tolerates_legacy_and_torn_lines(state_dir):
    """Same tolerance for the pending-detection handoff (shared _seen_shas)."""
    state_dir.mkdir(parents=True)
    watcher.write_watermark(state_dir, 5)
    pending = state_dir / "pending-detection.jsonl"
    pending.write_text('{"no_sha": true}\n{"sha": "ccc333"}\n{torn\n',
                       encoding="utf-8")
    games = [make_game(6, sha="ddd444"), make_game(7, sha="ccc333")]
    record, _ = run(state_dir, games)
    assert record["failures"] == []
    assert _pending_shas_tolerant(pending) == ["ccc333", "ddd444"]


def _pending_shas_tolerant(path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("sha"):
            out.append(obj["sha"])
    return out


# ------------------------------------------------------- queue mode + run log

def test_no_download_queues_to_jsonl_and_advances(state_dir):
    watcher.write_watermark(state_dir, 5)
    game = make_game(6, sha="abc123")
    record = watcher.run_watch(
        state_dir=state_dir, no_download=True,
        query_fn=lambda wm, lim: [game], max_rowid_fn=lambda: 6,
        out=open(state_dir.parent / "out.txt", "a"),
    )
    assert record["watermark_after"] == 6
    lines = (state_dir / "demos" / "queue.jsonl").read_text().strip().splitlines()
    entry = json.loads(lines[0])
    assert entry["sha"] == "abc123"
    assert entry["url"] == "https://d.quake.world/abc/abc123.mvd.gz"
    assert entry["map"] == "dm3"
    assert entry["teams"][0]["name"] == "blue"


def test_run_log_appended_per_invocation(state_dir):
    watcher.write_watermark(state_dir, 5)
    run(state_dir, [make_game(6)])
    run(state_dir, [make_game(6)])  # no-op second run
    lines = (state_dir / "watcher-runs.log").read_text().strip().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(l) for l in lines)
    assert first["watermark_before"] == 5 and first["watermark_after"] == 6
    assert second["found"] == 0 and second["watermark_after"] == 6
    for rec in (first, second):
        assert {"ts", "found", "processed", "failures"} <= set(rec)


def test_demo_url_format():
    sha = "8d8d69cc" + "0" * 56
    assert watcher.demo_url(sha) == f"https://d.quake.world/8d8/{sha}.mvd.gz"
