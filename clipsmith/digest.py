"""clipsmith weekly digest builder (pipeline-spec.md "Cadence" /
"Discord review loop", issue #11).

Assembles the Monday-20:00 digest JSON from the candidate store: all
candidates in status ``candidate`` whose match falls inside the digest week,
grouped by category, ranked by score, capped at the spec's max 3 per
category. The output document is the **Phase-2 Discord bot's input
contract** — keep the shape stable.

Week-boundary rule (which field governs, and why)
    The digest covers "this week's demos" — the matches *played* during the
    week, per the spec's intent ("candidates mined from fresh 4on4 demos",
    digest = the week's review surface). Therefore **``matchdate`` governs**
    membership: a candidate belongs to ISO week W when its ``matchdate``
    (YYYY-MM-DD, server-local date the match was played) falls within
    [W-Monday .. W-Sunday]. ``detected_at`` is an operational timestamp —
    detection runs 3x/week and may lag the match (or re-scan an old demo),
    so keying on it would shift moments into the wrong week whenever a
    detector run straddles midnight Sunday or backfills. ``detected_at`` is
    used only as a **fallback** when ``matchdate`` is "" (demo metadata had
    no parseable date): we then take the UTC date of ``detected_at`` so the
    candidate is not silently lost.

    The default week is the ISO week that ends at the upcoming (or current)
    Monday — i.e. the Monday-20:00 digest run covers the Mon..Sun week that
    has just finished. Mid-week builds (like validation runs) therefore
    default to the *current, still-running* ISO week.

Digest JSON shape (Phase-2 bot contract)
    {
      "week": "2026-W24",
      "week_start": "2026-06-08",          # Monday, inclusive
      "week_end": "2026-06-14",            # Sunday, inclusive
      "generated_at": "<ISO8601 UTC>",
      "sections": [                        # fixed category order, always all 4
                                           # (5 with COMEBACK enabled, below)
        {
          "category": "QUAD RUNNER",
          "label": "QUAD RUNNER OF THE WEEK — candidates",
          "note": null,                    # str when the section is empty
          "candidates": [                  # score desc, max 3, min 1 if any
            {
              "candidate_id": "...",
              "rank_in_category": 1,
              "score": 12.5,
              "pov_player": {"name": "...", "userid": 42},
              "map": "dm3",
              "matchdate": "2026-06-08",
              "teams": [{"name": "...", "score": 187}, ...],
              "window": {"start_s": 312.4, "end_s": 347.9},
              "description": "...",        # consumed as-is (issue #10 text)
              "hub_url": "https://hub.quakeworld.nu/games?..." or "",
              "hub_url_missing": false,    # true when hub_url == "" — the
                                           # Phase-2 poster retries hub_links
                                           # resolution before posting
              "metrics_summary": {...}     # metrics minus bulky lists; each
                                           # list key X becomes X_count
            }
          ]
        },
        ...
      ]
    }

    Empty-category marker (issue #11 task 3, "machine-distinguishable from
    an empty list caused by a bug"): an empty section has ``candidates: []``
    AND ``note`` set to a human sentence ("no qualifying <CATEGORY> moment
    this week"); a populated section has ``note: null``. A bug-caused empty
    list would lack the note. Never padded with weak candidates.

COMEBACK fifth category (issue #30 point 5 — default OFF)
    The COME-BACK detector ships behind an evaluation gate, so its digest
    section is opt-in: pass ``--include-comeback`` to ``build`` (or set
    ``CLIPSMITH_DIGEST_COMEBACK=1``; the CLI flag wins when given). When
    enabled, a fifth section ``COMEBACK`` is appended after the four
    Phase-1 categories with the same min-1/max-3 ranking and
    empty-section-note rules. When disabled (the default), the digest is
    byte-identical to the 4-section document — COMEBACK candidates in the
    store are simply not selected — so the Phase-2 bot contract is
    untouched until the owner reviews the eval-set precision and flips the
    flag.

Idempotency
    Rebuilding a week overwrites ``state/digests/<week>.json`` with stable
    ordering (sections in fixed category order; candidates by score desc,
    candidate id as tie-break) — only ``generated_at`` differs between
    reruns. ``mark-posted`` flips each digest candidate ``candidate ->
    posted`` via store.update_status; candidates already past ``candidate``
    (e.g. a second mark-posted run) are skipped with a notice per candidate,
    never a crash.

Usage:
    python -m clipsmith.digest build [--db PATH] [--week 2026-W24] [--out PATH]
        [--include-comeback]
    python -m clipsmith.digest mark-posted --week 2026-W24 [--db PATH]
        [--digest PATH]
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from clipsmith import store

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIGEST_DIR = REPO_ROOT / "state" / "digests"

# Fixed section order — the four Phase-1 categories from pipeline-spec.md
# ("Highlight categories"; COME-BACK is deferred to Phase 5).
CATEGORIES = ("QUAD RUNNER", "TEAM KILLER", "PENT GRIND", "PENT RUNNER")

# The fifth category (issue #30): appended only when the default-off flag
# is on — see the module docstring and comeback_enabled().
COMEBACK_CATEGORY = "COMEBACK"
COMEBACK_ENV_VAR = "CLIPSMITH_DIGEST_COMEBACK"
_ENV_TRUE = {"1", "true", "yes", "on"}

MAX_PER_CATEGORY = 3  # spec: min 1, max 3 candidates per category per week

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


# ---------------------------------------------------------------- week math

def parse_week(week_id):
    """'2026-W24' -> (monday_date, sunday_date), both inclusive."""
    m = _WEEK_RE.match(week_id or "")
    if not m:
        raise ValueError(
            f"invalid week id {week_id!r} (expected ISO week like 2026-W24)")
    year, week = int(m.group(1)), int(m.group(2))
    try:
        monday = dt.date.fromisocalendar(year, week, 1)
    except ValueError as e:
        raise ValueError(f"invalid week id {week_id!r}: {e}") from e
    return monday, monday + dt.timedelta(days=6)


def format_week(date):
    """date -> its ISO week id, e.g. '2026-W24'."""
    year, week, _ = date.isocalendar()
    return f"{year}-W{week:02d}"


def default_week(today=None):
    """The ISO week ending at the upcoming (or current) Monday.

    The Monday-20:00 digest covers the Mon..Sun week that finished the
    previous midnight; equivalently, the ISO week containing the day before
    the next Monday. Run on a Monday, that is the *previous* ISO week; run
    mid-week, it is the current (still-running) ISO week.
    """
    if today is None:
        today = dt.datetime.now(dt.timezone.utc).date()
    days_until_monday = (7 - today.weekday()) % 7  # 0 when today is Monday
    upcoming_monday = today + dt.timedelta(days=days_until_monday)
    return format_week(upcoming_monday - dt.timedelta(days=1))


def candidate_week_date(cand):
    """The date that places a candidate in a week (matchdate governs).

    Falls back to the UTC date of detected_at when matchdate is empty or
    unparseable; returns None if neither yields a date.
    """
    matchdate = cand.get("matchdate") or ""
    try:
        return dt.date.fromisoformat(matchdate)
    except ValueError:
        pass
    detected_at = cand.get("detected_at") or ""
    try:
        return dt.datetime.fromisoformat(detected_at).date()
    except ValueError:
        return None


# ------------------------------------------------------------ digest build

def metrics_summary(metrics):
    """Compact metrics for the digest embed: scalars kept, bulky lists
    replaced by '<key>_count' (the full metrics stay in the store for the
    Phase-3 kill-counter overlay)."""
    summary = {}
    for key, value in (metrics or {}).items():
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
        else:
            summary[key] = value
    return summary


def _entry(cand, rank):
    hub_url = cand.get("hub_url") or ""
    return {
        "candidate_id": cand["id"],
        "rank_in_category": rank,
        "score": cand["score"],
        "pov_player": cand["pov_player"],
        "map": cand["map"],
        "matchdate": cand["matchdate"],
        "teams": cand["teams"],
        "window": cand["window"],
        "description": cand["description"],
        "hub_url": hub_url,
        "hub_url_missing": hub_url == "",
        "metrics_summary": metrics_summary(cand.get("metrics")),
    }


def comeback_enabled(flag=None):
    """The COMEBACK fifth-category switch (default OFF — issue #30).

    An explicit ``flag`` (the CLI's --include-comeback) wins; otherwise the
    CLIPSMITH_DIGEST_COMEBACK env var enables it when set to a truthy value
    (1/true/yes/on, case-insensitive).
    """
    if flag is not None:
        return bool(flag)
    return os.environ.get(COMEBACK_ENV_VAR, "").strip().lower() in _ENV_TRUE


def build_digest(week_id, db_path=None, now=None, include_comeback=None):
    """Build the digest document for week_id from the candidate store.

    ``include_comeback``: None (default) defers to comeback_enabled() —
    i.e. the CLIPSMITH_DIGEST_COMEBACK env var, default off; True/False
    force the COMEBACK section on/off.
    """
    week_start, week_end = parse_week(week_id)
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)

    in_week = [
        c for c in store.list_candidates(status="candidate", db_path=db_path)
        if (d := candidate_week_date(c)) is not None
        and week_start <= d <= week_end
    ]

    categories = CATEGORIES
    if comeback_enabled(include_comeback):
        categories = CATEGORIES + (COMEBACK_CATEGORY,)

    sections = []
    for category in categories:
        ranked = sorted(
            (c for c in in_week if c["category"] == category),
            key=lambda c: (-c["score"], c["id"]),  # id tie-break: stable
        )[:MAX_PER_CATEGORY]
        sections.append({
            "category": category,
            "label": f"{category} OF THE WEEK — candidates",
            "note": (None if ranked
                     else f"no qualifying {category} moment this week"),
            "candidates": [_entry(c, rank)
                           for rank, c in enumerate(ranked, start=1)],
        })

    return {
        "week": week_id,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "generated_at": now.isoformat(),
        "sections": sections,
    }


def default_digest_path(week_id):
    return DEFAULT_DIGEST_DIR / f"{week_id}.json"


def write_digest(digest, out_path):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(digest, indent=2, ensure_ascii=False)
                        + "\n", encoding="utf-8")
    return out_path


# ------------------------------------------------------------- mark-posted

def digest_candidate_ids(digest):
    """All candidate ids in a digest document, in section order."""
    return [entry["candidate_id"]
            for section in digest.get("sections", [])
            for entry in section.get("candidates", [])]


def mark_posted(week_id, db_path=None, digest_path=None):
    """Flip the week's digest candidates candidate -> posted.

    Reads the digest JSON (the exact document the bot posted), then per
    candidate: ``candidate`` -> posted; any other current status (already
    posted, rejected meanwhile, ...) -> skipped with a notice. Returns
    [(candidate_id, action)] where action is 'posted' or
    'skipped (status=<s>)'. Missing candidates raise ValueError (a digest
    pointing at ids not in the store is a real inconsistency).
    """
    path = Path(digest_path) if digest_path else default_digest_path(week_id)
    if not path.exists():
        raise ValueError(
            f"no digest for week {week_id} at {path} — run build first")
    digest = json.loads(path.read_text(encoding="utf-8"))

    results = []
    for cand_id in digest_candidate_ids(digest):
        current = store.get_candidate(cand_id, db_path=db_path)["status"]
        if current == "candidate":
            store.update_status(cand_id, "posted", db_path=db_path)
            results.append((cand_id, "posted"))
        else:
            results.append((cand_id, f"skipped (status={current})"))
    return results


# -------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipsmith.digest",
        description="clipsmith weekly digest builder (issue #11)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser(
        "build", help="build the week's digest JSON (write + echo to stdout)")
    p_build.add_argument("--db", help="candidate store path "
                         "(default: state/candidates.db, or $CLIPSMITH_DB)")
    p_build.add_argument("--week", help="ISO week id like 2026-W24 "
                         "(default: the week ending the upcoming/current Monday)")
    p_build.add_argument("--out", help="output path "
                         "(default: state/digests/<week>.json)")
    p_build.add_argument("--include-comeback", action="store_true",
                         default=None,
                         help="append the COMEBACK fifth section (default "
                         "off until the issue #30 eval gate passes; also "
                         f"enabled by {COMEBACK_ENV_VAR}=1)")

    p_mark = sub.add_parser(
        "mark-posted",
        help="flip a built digest's candidates candidate -> posted")
    p_mark.add_argument("--week", required=True, help="ISO week id like 2026-W24")
    p_mark.add_argument("--db", help="candidate store path")
    p_mark.add_argument("--digest", help="digest JSON path "
                        "(default: state/digests/<week>.json)")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "build":
            week_id = args.week or default_week()
            parse_week(week_id)  # validate --week early
            digest = build_digest(week_id, db_path=args.db,
                                  include_comeback=args.include_comeback)
            out_path = args.out or default_digest_path(week_id)
            write_digest(digest, out_path)
            print(json.dumps(digest, indent=2, ensure_ascii=False))
            print(f"digest written: {out_path}", file=sys.stderr)
        elif args.cmd == "mark-posted":
            results = mark_posted(args.week, db_path=args.db,
                                  digest_path=args.digest)
            if not results:
                print(f"no candidates in digest {args.week}; nothing to do",
                      file=sys.stderr)
            for cand_id, action in results:
                print(f"{cand_id}: {action}")
    except (ValueError, json.JSONDecodeError, OSError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
