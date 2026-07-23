"""clipsmith new-demo watcher — incremental match discovery with a watermark.

The production adapter queries Xerial's private QuakeWorld stats database,
currently hosted on Turso/libSQL (HTTP /v2/pipeline API), for 4on4 games newer
than a persisted watermark. It records sha / map / matchdate / teams for each
and downloads (or queues) the demo from the d.quake.world CDN. This database
is not a public clipsmith dependency: ``run_watcher`` accepts injected query
functions so another source can provide the same normalized game records and
cursor semantics.

Watermark
    The watermark is the `games` table **rowid** (insertion order). rowid is
    monotonic with arrival, so demos that surface late with an old matchdate
    are still caught — a matchdate watermark would skip them. Persisted as
    JSON in state/watcher-watermark.json. It advances only past the games
    that were processed successfully: on a partial failure the new watermark
    stops just before the first failed game, so the next run retries it
    (downloads are idempotent — existing files are skipped).

Cold start
    With no watermark file the watcher initializes the watermark to the
    current MAX(rowid) without processing history (110k+ games). Use
    --reset-watermark to rewind afterwards if backfill is wanted.

Crash & partial-failure recovery (issue #32 hardening)
    The pipeline runs unattended 3x/week; every run must be safely
    re-runnable. Guarantees, in order of the run:

    - Write-ahead intent: before processing a batch the watcher records the
      scan target in state/watcher-inprogress.json (started_at +
      watermark_before + batch rowid range). The marker is removed only when
      the batch loop completes; a leftover marker on the next run means the
      previous run crashed mid-batch. The durable watermark is written only
      AFTER the batch loop, so a crash at any point leaves it at its pre-run
      value and the rerun re-processes the whole batch. Exception: a crash
      in the window after the watermark write but before the marker clear
      leaves a STALE marker — the next run detects this (current watermark
      >= the marker's batch_last_rowid), clears the marker, and reports the
      batch as completed rather than re-processed.
    - Rerun convergence: downloads skip existing files, the
      pending-detection handoff and queue.jsonl are sha-deduped — so a
      crashed run + rerun ends in exactly the same state as one
      uninterrupted run.
    - Watermark non-regression: write_watermark refuses to move the
      watermark backwards except through the explicit --reset-watermark
      path (reason="manual-reset").
    - Corrupt watermark file: read_watermark raises instead of treating an
      unreadable file as a cold start (a cold start would silently skip the
      backlog).
    - Resumable from: any crash point. A rerun re-fetches the frozen batch
      (skip for already-downloaded demos, redo for the rest). The watermark
      means "fetched + queued for detection" — detector work is consumed
      from state/pending-detection.jsonl independently, so detector crashes
      never lose demos the watermark already passed.

Credentials
    TURSO_DB_URL + TURSO_AUTH_TOKEN_QWSTATS are read from
    ~/.config/api-keys.env (KEY=VALUE lines). Values are never printed.

Usage:
    python -m clipsmith.watcher                      # poll + download new demos
    python -m clipsmith.watcher --no-download        # poll + queue only
    python -m clipsmith.watcher --dry-run            # print plan; write nothing
    python -m clipsmith.watcher --reset-watermark N  # rewind/replay
"""
import argparse
import gzip
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENV_PATH = os.path.expanduser("~/.config/api-keys.env")
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
CDN_BASE = "https://d.quake.world"
DEFAULT_LIMIT = 500

GAME_FIELDS = ("rowid", "sha256", "map", "date", "team1", "team2", "score1", "score2")


# ---------------------------------------------------------------- credentials

def load_creds(env_path=ENV_PATH):
    """Parse KEY=VALUE lines; return (https_url, token). Never print values."""
    url = token = None
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "TURSO_DB_URL":
                url = v
            elif k == "TURSO_AUTH_TOKEN_QWSTATS":
                token = v
    if not url or not token:
        raise RuntimeError(
            "missing TURSO_DB_URL or TURSO_AUTH_TOKEN_QWSTATS in " + str(env_path)
        )
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url.rstrip("/"), token


# ----------------------------------------------------------------- turso HTTP

def run_sql(url, token, sql, args=()):
    """Execute one parameterized statement over the libSQL pipeline API."""
    stmt = {"sql": sql}
    if args:
        stmt["args"] = [
            {"type": "integer", "value": str(a)} if isinstance(a, int)
            else {"type": "text", "value": str(a)}
            for a in args
        ]
    body = json.dumps({
        "requests": [
            {"type": "execute", "stmt": stmt},
            {"type": "close"},
        ]
    }).encode()
    req = urllib.request.Request(
        url + "/v2/pipeline",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    res = data["results"][0]
    if res.get("type") != "ok":
        raise RuntimeError("turso query error: " + json.dumps(res)[:500])
    r = res["response"]["result"]
    cols = [c["name"] for c in r["cols"]]
    return [
        {cols[i]: cell.get("value") for i, cell in enumerate(row)}
        for row in r["rows"]
    ]


def query_max_rowid(url, token):
    rows = run_sql(url, token, "SELECT COALESCE(MAX(rowid), 0) AS m FROM games")
    return int(rows[0]["m"])


def query_new_games(url, token, since_rowid, limit=DEFAULT_LIMIT):
    """4on4 games with rowid > since_rowid, oldest-arrival first."""
    rows = run_sql(
        url, token,
        "SELECT rowid, sha256, map, date, team1, team2, score1, score2 "
        "FROM games WHERE mode = '4on4' AND rowid > ? ORDER BY rowid LIMIT ?",
        (int(since_rowid), int(limit)),
    )
    games = []
    for r in rows:
        games.append({
            "rowid": int(r["rowid"]),
            "sha": r["sha256"],
            "map": r["map"],
            "matchdate": r["date"],
            "teams": [
                {"name": r["team1"], "score": int(r["score1"])},
                {"name": r["team2"], "score": int(r["score2"])},
            ],
        })
    return games


# ------------------------------------------------------------------ watermark

def watermark_path(state_dir):
    return Path(state_dir) / "watcher-watermark.json"


def read_watermark(state_dir):
    """Return the persisted rowid watermark, or None if not initialized.

    A present-but-unreadable file raises RuntimeError: it must NEVER be
    treated as a cold start, because a cold start re-initializes to head and
    silently skips the entire backlog.
    """
    path = watermark_path(state_dir)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return int(json.load(f)["rowid"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise RuntimeError(
            f"watermark file {path} is unreadable ({e}); refusing to treat it "
            "as a cold start (that would skip the backlog). Inspect the file "
            "or rewind explicitly with --reset-watermark."
        ) from e


def write_watermark(state_dir, rowid, reason="run", allow_rewind=False):
    """Atomically persist the watermark. Refuses to move it backwards unless
    ``allow_rewind`` (the explicit --reset-watermark path) is set."""
    path = watermark_path(state_dir)
    if not allow_rewind and path.exists():
        current = read_watermark(state_dir)
        if int(rowid) < current:
            raise RuntimeError(
                f"watermark regression refused: {current} -> {int(rowid)} "
                "(use --reset-watermark to rewind explicitly)"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "rowid": int(rowid),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------- write-ahead intent marker

def inprogress_path(state_dir):
    return Path(state_dir) / "watcher-inprogress.json"


def read_inprogress(state_dir):
    """Return the leftover in-progress marker (= previous run crashed
    mid-batch), or None. An unparseable marker still signals a crash."""
    path = inprogress_path(state_dir)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return {"corrupt": True}


def write_inprogress(state_dir, info):
    """Atomically record the scan target BEFORE processing the batch."""
    path = inprogress_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def clear_inprogress(state_dir):
    inprogress_path(state_dir).unlink(missing_ok=True)


# ----------------------------------------------------------- download / queue

def _seen_shas(jsonl_path):
    """Shas already present in a jsonl state file, for append dedup.

    Tolerant of unkeyable lines (a torn append from a crash mid-write, hand
    edits, foreign producers): a line that is not a JSON object with a "sha"
    key is skipped — it cannot be deduped against, but it must never crash
    the run and break the watcher on existing state (issue #32 review P1).
    """
    seen = set()
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("sha"):
                seen.add(obj["sha"])
    return seen


def demo_url(sha):
    return f"{CDN_BASE}/{sha[:3]}/{sha}.mvd.gz"


def download_demo(game, demos_dir):
    """Fetch <sha>.mvd.gz into demos_dir. Idempotent: skips existing files.

    Returns the local path. Raises on network/HTTP errors and on payloads
    that are not gzip (CDN error pages must not advance the watermark).
    """
    demos_dir = Path(demos_dir)
    demos_dir.mkdir(parents=True, exist_ok=True)
    dest = demos_dir / f"{game['sha']}.mvd.gz"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(".gz.part")
    req = urllib.request.Request(demo_url(game["sha"]),
                                 headers={"User-Agent": "clipsmith-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    with open(tmp, "rb") as f:
        if f.read(2) != b"\x1f\x8b":
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{game['sha']}: response is not gzip")
    with gzip.open(tmp, "rb") as f:  # cheap integrity probe of the stream head
        f.read(1024)
    os.replace(tmp, dest)
    return dest


def queue_demo(game, demos_dir):
    """--no-download mode: append the demo to state/demos/queue.jsonl.

    Idempotent: entries are deduped by sha so a crashed/rewound run that is
    re-run does not double-queue a demo. Unkeyable lines in an existing
    queue file are skipped, never a crash (see :func:`_seen_shas`).
    """
    demos_dir = Path(demos_dir)
    demos_dir.mkdir(parents=True, exist_ok=True)
    queue_path = demos_dir / "queue.jsonl"
    if queue_path.exists() and game["sha"] in _seen_shas(queue_path):
        return queue_path
    entry = {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "sha": game["sha"],
        "url": demo_url(game["sha"]),
        "map": game["map"],
        "matchdate": game["matchdate"],
        "teams": game["teams"],
    }
    with open(queue_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return queue_path


def append_pending_detection(state_dir, game):
    """Durable watcher → detection handoff: state/pending-detection.jsonl.

    The watermark only means "fetched" — the detection stage consumes this
    file (and removes entries) independently, so a detector crash can never
    lose a demo that the watermark has already advanced past. Entries are
    deduped by sha so a rewound/replayed run doesn't double-queue; unkeyable
    lines are skipped, never a crash (see :func:`_seen_shas`).
    """
    path = Path(state_dir) / "pending-detection.jsonl"
    if path.exists() and game["sha"] in _seen_shas(path):
        return path
    entry = {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "rowid": game["rowid"],
        "sha": game["sha"],
        "map": game["map"],
        "matchdate": game["matchdate"],
        "teams": game["teams"],
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


# -------------------------------------------------------------------- run log

def append_run_log(state_dir, record):
    path = Path(state_dir) / "watcher-runs.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------- the run

def run_watch(state_dir=STATE_DIR, env_path=ENV_PATH, no_download=False,
              limit=DEFAULT_LIMIT, query_fn=None, max_rowid_fn=None,
              download_fn=None, out=None, dry_run=False):
    """One watcher invocation. Returns the run-log record (also persisted).

    Watermark semantics: advances to the rowid of the last game in the
    contiguous successfully-processed prefix; the first failure freezes it,
    so unprocessed demos are re-seen next run. Remaining games are still
    attempted (downloads are idempotent), but cannot advance the watermark.
    The single watermark write happens only AFTER the batch loop, behind a
    write-ahead marker (state/watcher-inprogress.json) recorded before the
    batch — a crash anywhere mid-run leaves the watermark at its pre-run
    value and the leftover marker flags the crash on the next run.

    "Processed" means fetched + queued for detection: every successful game
    is also appended to state/pending-detection.jsonl (sha-deduped), the
    durable handoff the detection stage consumes — so the watermark advancing
    never strands a demo whose detectors haven't run yet.

    ``dry_run=True`` prints what the run would do (Turso is still queried,
    read-only) but writes NOTHING: no downloads, no queue/handoff appends,
    no run-log entry, no marker, and the watermark does not move.
    """
    out = out or sys.stdout
    state_dir = Path(state_dir)
    demos_dir = state_dir / "demos"
    started = datetime.now(timezone.utc).isoformat()

    if query_fn is None or max_rowid_fn is None:
        url, token = load_creds(env_path)
        query_fn = query_fn or (lambda wm, lim: query_new_games(url, token, wm, lim))
        max_rowid_fn = max_rowid_fn or (lambda: query_max_rowid(url, token))
    if download_fn is None:
        download_fn = queue_demo if no_download else download_demo

    before = read_watermark(state_dir)  # raises on a corrupt watermark file

    # Leftover write-ahead marker = the previous run crashed. Usually that
    # means mid-batch (watermark not advanced; this run re-processes the
    # batch) — but a crash in the window AFTER the watermark write and BEFORE
    # the marker clear leaves a STALE marker: the batch completed, and
    # claiming it "will be re-processed" would be a lie (the advanced
    # watermark means it won't be). Distinguish via the current watermark.
    recovered = read_inprogress(state_dir)
    if recovered is not None:
        batch_last = recovered.get("batch_last_rowid")
        if before is not None and batch_last is not None and before >= batch_last:
            recovered = {**recovered, "stale": True}
            msg = (f"previous run (started {recovered.get('started_at', '?')}) "
                   f"completed its batch (watermark {before} >= batch end "
                   f"{batch_last}) but died before clearing its marker; "
                   f"marker was stale")
            if dry_run:
                print(msg + " (dry run: marker left in place)", file=out)
            else:
                clear_inprogress(state_dir)
                print(msg + " and has been cleared", file=out)
        else:
            print(f"recovering from interrupted run "
                  f"(started {recovered.get('started_at', '?')}): watermark "
                  f"intact at {before}; the batch will be re-processed", file=out)

    if before is None:
        # Cold start: initialize to current head, process nothing.
        head = max_rowid_fn()
        if dry_run:
            record = {
                "ts": started, "event": "cold_start", "dry_run": True,
                "watermark_before": None, "watermark_after": None,
                "would_initialize_to": head,
                "found": 0, "processed": 0, "failures": [],
                "no_download": no_download,
            }
            print(f"dry run: would initialize watermark to rowid {head} "
                  f"(nothing written)", file=out)
            return record
        write_watermark(state_dir, head, reason="cold-start-init")
        clear_inprogress(state_dir)
        record = {
            "ts": started, "event": "cold_start", "dry_run": False,
            "watermark_before": None, "watermark_after": head,
            "found": 0, "processed": 0, "failures": [],
            "no_download": no_download,
        }
        append_run_log(state_dir, record)
        print(f"cold start: watermark initialized to rowid {head} "
              f"(no demos processed; use --reset-watermark to backfill)", file=out)
        return record

    games = query_fn(before, limit)

    if dry_run:
        verb = "would queue" if no_download else "would download"
        for game in games:
            teams = " vs ".join(f"{t['name']} {t['score']}" for t in game["teams"])
            print(f"  {verb}  rowid={game['rowid']} sha={game['sha']} "
                  f"map={game['map']} matchdate={game['matchdate']} [{teams}]",
                  file=out)
        record = {
            "ts": started, "event": "run", "dry_run": True,
            "watermark_before": before, "watermark_after": before,
            "found": len(games), "processed": 0, "failures": [],
            "no_download": no_download,
            "recovered_inprogress": recovered,
        }
        print(f"dry run: found={len(games)} (nothing written; "
              f"watermark stays at {before})", file=out)
        return record

    if games:
        # Write-ahead: record the scan target BEFORE any processing.
        write_inprogress(state_dir, {
            "started_at": started,
            "watermark_before": before,
            "batch_first_rowid": games[0]["rowid"],
            "batch_last_rowid": games[-1]["rowid"],
            "found": len(games),
        })

    processed, failures = [], []
    advance_to = before
    frozen = False
    for game in games:
        try:
            download_fn(game, demos_dir)
            append_pending_detection(state_dir, game)
        except Exception as e:  # noqa: BLE001 — any failure must freeze the watermark
            failures.append({"rowid": game["rowid"], "sha": game["sha"],
                             "error": str(e)})
            frozen = True
            print(f"  FAIL  rowid={game['rowid']} sha={game['sha']}: {e}", file=out)
            continue
        processed.append(game)
        if not frozen:
            advance_to = game["rowid"]
        teams = " vs ".join(f"{t['name']} {t['score']}" for t in game["teams"])
        verb = "queued" if no_download else "downloaded"
        print(f"  {verb}  sha={game['sha']} map={game['map']} "
              f"matchdate={game['matchdate']} [{teams}]", file=out)

    if advance_to != before:
        write_watermark(state_dir, advance_to, reason="run")
    # Batch loop completed (even with per-game failures, which are recorded
    # below and retried next run) — the crash marker comes off.
    clear_inprogress(state_dir)

    record = {
        "ts": started,
        "event": "run",
        "dry_run": False,
        "watermark_before": before,
        "watermark_after": advance_to,
        "found": len(games),
        "processed": len(processed),
        "failures": failures,
        "no_download": no_download,
        "recovered_inprogress": recovered,
    }
    append_run_log(state_dir, record)
    print(f"watcher: found={len(games)} processed={len(processed)} "
          f"failed={len(failures)} watermark {before} -> {advance_to}", file=out)
    return record


def reset_watermark(state_dir, value, out=None):
    """Explicit rewind/replay path — the ONLY way to move the watermark
    backwards. Also the recovery path for a corrupt watermark file."""
    out = out or sys.stdout
    state_dir = Path(state_dir)
    try:
        before = read_watermark(state_dir)
    except RuntimeError:
        before = "corrupt"
    write_watermark(state_dir, int(value), reason="manual-reset",
                    allow_rewind=True)
    append_run_log(state_dir, {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "reset_watermark",
        "watermark_before": before,
        "watermark_after": int(value),
    })
    print(f"watermark reset: {before} -> {int(value)}", file=out)


# ----------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipsmith.watcher",
        description=("Poll Xerial's private Turso/libSQL stats source for "
                     "new 4on4 demos since the persisted watermark."),
    )
    parser.add_argument("--no-download", action="store_true",
                        help="queue demos to state/demos/queue.jsonl instead of downloading")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would happen; write nothing, advance no watermark")
    parser.add_argument("--reset-watermark", metavar="ROWID", type=int,
                        help="rewind the watermark to this games.rowid and exit")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"max games per run (default {DEFAULT_LIMIT})")
    parser.add_argument("--state-dir", default=str(STATE_DIR),
                        help="state directory (default: <repo>/state)")
    parser.add_argument("--env-file", default=ENV_PATH,
                        help="credentials env file (default: ~/.config/api-keys.env)")
    args = parser.parse_args(argv)

    if args.reset_watermark is not None:
        try:
            if args.dry_run:
                try:
                    before = read_watermark(args.state_dir)
                except RuntimeError:
                    before = "corrupt"
                print(f"dry run: would reset watermark "
                      f"{before} -> {args.reset_watermark} (nothing written)")
            else:
                reset_watermark(args.state_dir, args.reset_watermark)
        except (RuntimeError, OSError) as e:
            sys.exit(f"error: {e}")
        return 0

    try:
        record = run_watch(state_dir=args.state_dir, env_path=args.env_file,
                           no_download=args.no_download, limit=args.limit,
                           dry_run=args.dry_run)
    except (RuntimeError, OSError, urllib.error.URLError) as e:
        sys.exit(f"error: {e}")
    return 1 if record["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
