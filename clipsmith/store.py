"""clipsmith candidate store — SQLite-backed, the pipeline's spine.

Implements the candidate contract from docs/pipeline-spec.md: every detector
emits candidates in this schema, every later stage (digest, Discord selection,
render, upload, approval, publish) reads and writes it.

Candidate fields:
    id, category, detected_at, demo_sha, map, matchdate,
    teams (names+scores), pov_player {name, userid},
    window {start_s, end_s} (match-relative seconds),
    score, metrics {...} (must round-trip per-kill event timestamps
    losslessly — the Phase-3 kill-counter overlay consumes them),
    description, hub_url, status

Status lifecycle (from pipeline-spec.md, incl. the Discord review loop):

    from                 -> allowed next
    -------------------------------------------------
    candidate            -> posted, rejected
    posted               -> selected, rejected
    selected             -> posted, rendered   (posted = Discord re-selection
                                                replaces this candidate before
                                                render starts)
    rendered             -> uploaded
    uploaded             -> approved, revision_requested
    approved             -> published
    revision_requested   -> rendered   (re-render loop: a revision always
                                        re-renders before re-uploading)
    published            -> (terminal)
    rejected             -> (terminal)

Illegal transitions raise ValueError (CLI exits non-zero).

DB location: state/candidates.db under the repo root by default; override
with the CLIPSMITH_DB env var or the --db CLI flag.

Usage:
    python -m clipsmith.store add [candidate.json]      # or JSON on stdin; prints id
    python -m clipsmith.store list [--category C] [--status S]   # JSON lines
    python -m clipsmith.store update-status <id> <status>
    python -m clipsmith.store get <id>                  # full candidate JSON
"""
import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "state" / "candidates.db"

# JSON-valued fields stored as serialized TEXT columns.
JSON_FIELDS = ("teams", "pov_player", "window", "metrics")

FIELDS = (
    "id", "category", "detected_at", "demo_sha", "map", "matchdate",
    "teams", "pov_player", "window", "score", "metrics",
    "description", "hub_url", "status",
)

TRANSITIONS = {
    "candidate": {"posted", "rejected"},
    "posted": {"selected", "rejected"},
    "selected": {"posted", "rendered"},
    "rendered": {"uploaded"},
    "uploaded": {"approved", "revision_requested"},
    "approved": {"published"},
    "revision_requested": {"rendered"},
    "published": set(),
    "rejected": set(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id          TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    demo_sha    TEXT NOT NULL,
    map         TEXT NOT NULL,
    matchdate   TEXT NOT NULL,
    teams       TEXT NOT NULL,
    pov_player  TEXT NOT NULL,
    window      TEXT NOT NULL,
    -- NUMERIC affinity keeps ints as ints; whole floats (6.0) canonicalize
    -- to ints (6), which still compare == in Python. Exact float fidelity
    -- where it matters (kill timestamps) lives in the JSON columns.
    score       NUMERIC NOT NULL,
    metrics     TEXT NOT NULL,
    description TEXT NOT NULL,
    hub_url     TEXT NOT NULL,
    status      TEXT NOT NULL
)
"""


def resolve_db_path(db_path=None):
    if db_path:
        return Path(db_path)
    env = os.environ.get("CLIPSMITH_DB")
    if env:
        return Path(env)
    return DEFAULT_DB


def connect(db_path=None):
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    return conn


def _row_to_candidate(row):
    cand = dict(zip(FIELDS, row))
    for f in JSON_FIELDS:
        cand[f] = json.loads(cand[f])
    return cand


def add_candidate(cand, db_path=None):
    """Insert a candidate dict; fills id/detected_at/status defaults. Returns id."""
    cand = dict(cand)
    cand.setdefault("id", uuid.uuid4().hex)
    cand.setdefault("detected_at", datetime.now(timezone.utc).isoformat())
    cand.setdefault("status", "candidate")
    if cand["status"] not in TRANSITIONS:
        raise ValueError(f"unknown status: {cand['status']!r}")
    missing = [f for f in FIELDS if f not in cand]
    if missing:
        raise ValueError(f"candidate missing fields: {', '.join(missing)}")
    unknown = [k for k in cand if k not in FIELDS]
    if unknown:
        raise ValueError(f"unknown candidate fields: {', '.join(unknown)}")
    row = [
        json.dumps(cand[f], ensure_ascii=False) if f in JSON_FIELDS else cand[f]
        for f in FIELDS
    ]
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                f"INSERT INTO candidates ({', '.join(FIELDS)}) "
                f"VALUES ({', '.join('?' * len(FIELDS))})",
                row,
            )
    finally:
        conn.close()
    return cand["id"]


def get_candidate(cand_id, db_path=None):
    conn = connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {', '.join(FIELDS)} FROM candidates WHERE id = ?", (cand_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"no candidate with id {cand_id!r}")
    return _row_to_candidate(row)


def list_candidates(category=None, status=None, db_path=None):
    where, params = [], []
    if category:
        where.append("category = ?")
        params.append(category)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = f"SELECT {', '.join(FIELDS)} FROM candidates"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY detected_at, id"
    conn = connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_candidate(r) for r in rows]


def update_status(cand_id, new_status, db_path=None):
    """Move a candidate to new_status, enforcing the lifecycle table."""
    if new_status not in TRANSITIONS:
        raise ValueError(f"unknown status: {new_status!r}")
    conn = connect(db_path)
    try:
        with conn:
            row = conn.execute(
                "SELECT status FROM candidates WHERE id = ?", (cand_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"no candidate with id {cand_id!r}")
            current = row[0]
            if new_status not in TRANSITIONS[current]:
                raise ValueError(
                    f"illegal transition: {current!r} -> {new_status!r}"
                )
            conn.execute(
                "UPDATE candidates SET status = ? WHERE id = ?",
                (new_status, cand_id),
            )
    finally:
        conn.close()


def set_hub_url(cand_id, hub_url, db_path=None):
    """Set a candidate's hub_url ("" = not on hub yet; resolver may retry).

    Added for clipsmith.hub_links (issue #9); status is untouched.
    """
    conn = connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE candidates SET hub_url = ? WHERE id = ?",
                (hub_url, cand_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"no candidate with id {cand_id!r}")
    finally:
        conn.close()


def set_description(cand_id, description, db_path=None):
    """Set a candidate's description (describer rewrite pass).

    Added for clipsmith.describer (issue #10), mirroring set_hub_url;
    status is untouched.
    """
    conn = connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE candidates SET description = ? WHERE id = ?",
                (description, cand_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"no candidate with id {cand_id!r}")
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipsmith.store",
        description="clipsmith candidate store CRUD",
    )
    parser.add_argument("--db", help="DB path (default: state/candidates.db, or $CLIPSMITH_DB)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="add a candidate from JSON file or stdin; prints id")
    p_add.add_argument("file", nargs="?", help="JSON file (default: stdin)")

    p_list = sub.add_parser("list", help="list candidates as JSON lines")
    p_list.add_argument("--category")
    p_list.add_argument("--status")

    p_upd = sub.add_parser("update-status", help="move a candidate to a new status")
    p_upd.add_argument("id")
    p_upd.add_argument("status")

    p_get = sub.add_parser("get", help="print full candidate JSON")
    p_get.add_argument("id")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "add":
            if args.file:
                with open(args.file, encoding="utf-8") as f:
                    cand = json.load(f)
            else:
                cand = json.load(sys.stdin)
            print(add_candidate(cand, db_path=args.db))
        elif args.cmd == "list":
            for cand in list_candidates(args.category, args.status, db_path=args.db):
                print(json.dumps(cand, ensure_ascii=False))
        elif args.cmd == "update-status":
            update_status(args.id, args.status, db_path=args.db)
        elif args.cmd == "get":
            print(json.dumps(get_candidate(args.id, db_path=args.db),
                             indent=2, ensure_ascii=False))
    except (ValueError, json.JSONDecodeError, OSError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
