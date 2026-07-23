"""QUAD RUNNER OF THE WEEK detector (pipeline-spec.md category 1, issue #5).

Given a demo sha, enumerates quad runs via getItems(items=quad) phases,
scores each run from the getFrags kill log, cross-checks against
PowerupEvents[].frags, and emits candidates into the candidate store.

Run enumeration
    Each ``items=quad`` phase (``takenAt``/``takenBy``) is one run. The held
    interval is [takenAt, expiry]: expiry comes from the matching powerup
    event's ``detail.endTime`` (PowerupEvents — joined on holder name and
    pickup time within ``EVENT_JOIN_TOLERANCE_MS``); if no event matches, it
    falls back to takenAt + 30 s (the quad powerup duration), capped at
    match end. The matched event's ``detail.frags`` doubles as the
    cross-check count (WARN on mismatch with the computed count, never fail).

Scoring formula (documented per issue #5 point 3)
    ``score = frags + (RL_BONUS_PER_KILL * rl_kills if rl_kills >= RL_BONUS_MIN_KILLS else 0)``

    - ``frags``: kills by the quad holder during the held interval, from
      getFrags, excluding suicides and teamkills.
    - RL bonus: a run with >= 2 rocket-launcher kills earns
      +0.25 per RL kill. The per-kill bonus is deliberately well below 1.0
      so that style weighting can break ties between near-equal runs but a
      genuinely higher frag count is very hard to outrank (a 6-frag/3-RL run
      scores 6.75, below a 7-frag run's 7.0).
    - Ties on score are broken by raw frag count (desc), then earlier pickup.

Window
    pickup −5 s … +30 s, extended to the end of the run while the holder
    keeps fragging past +30 s — pure function ``common.compute_window``,
    unit-tested offline in tests/test_quad_runner.py.

CLI
    python -m clipsmith.detectors.quad_runner --sha <sha> [--db PATH]
        [--api-url URL] [--limit N] [--min-frags N] [--dry-run]

    --dry-run prints candidate JSON without writing to the store.
"""
import argparse
import logging
import sys

from clipsmith.detectors import common

log = logging.getLogger("clipsmith.detectors.quad_runner")

CATEGORY = "QUAD RUNNER"

QUAD_DURATION_S = 30.0          # quad powerup lifetime (fallback expiry)
EVENT_JOIN_TOLERANCE_MS = 2000  # phase.takenAt <-> PowerupEvent.t join slack
RL_BONUS_MIN_KILLS = 2          # bonus only when the run has >= 2 RL kills
RL_BONUS_PER_KILL = 0.25        # see scoring formula in module docstring


def score_run(frag_count, rl_kills):
    """Score per the module-docstring formula. Pure."""
    bonus = RL_BONUS_PER_KILL * rl_kills if rl_kills >= RL_BONUS_MIN_KILLS else 0.0
    return frag_count + bonus


def _mmss(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _match_powerup_event(events, holder, taken_at_ms):
    """Find the quad PowerupEvent for a phase (same holder, pickup within
    tolerance). Returns the event dict or None."""
    best = None
    for e in events:
        if e.get("type") != "powerup" or e.get("player") != holder:
            continue
        detail = e.get("detail") or {}
        if detail.get("powerup") != "quad":
            continue
        dt = abs(e["t"] * 1000.0 - taken_at_ms)
        if dt <= EVENT_JOIN_TOLERANCE_MS and (best is None or dt < best[0]):
            best = (dt, e)
    return best[1] if best else None


def detect(client, sha, min_frags=1):
    """Analyze one demo; return scored QUAD RUNNER candidates, best first.

    Candidates follow the store contract minus id/detected_at/status (the
    store fills those). Logs a WARNING when the computed frag count differs
    from PowerupEvents[].frags.
    """
    client.load_demo(sha)
    overview = client.get_overview(sha)
    metadata = client.get_metadata(sha)
    meta = common.demo_meta(overview, metadata)

    frags = client.get_frags(sha).get("frags") or []
    items = client.get_items(sha, items="quad").get("items") or []
    events = client.get_events(sha, types="powerup").get("events") or []

    match_end_ms = overview.get("matchEnd") or 0

    phases = []
    for item in items:
        if item.get("kind") != "quad" and item.get("name") != "quad":
            continue
        phases.extend(p for p in (item.get("phases") or []) if p.get("takenBy"))

    runs = []
    for phase in phases:
        holder = phase["takenBy"]
        taken_at_ms = phase["takenAt"]

        event = _match_powerup_event(events, holder, taken_at_ms)
        if event is not None:
            run_end_ms = event["detail"]["endTime"] * 1000.0
            api_frags = event["detail"].get("frags")
        else:
            run_end_ms = taken_at_ms + QUAD_DURATION_S * 1000.0
            if match_end_ms:
                run_end_ms = min(run_end_ms, match_end_ms)
            api_frags = None

        kills = common.filter_kills(frags, holder, taken_at_ms, run_end_ms)
        excluded = [f for f in common.filter_kills(
            frags, holder, taken_at_ms, run_end_ms,
            include_teamkills=True, include_suicides=True) if f not in kills]
        suicides_excluded = sum(1 for f in excluded if f.get("isSuicide", False))
        teamkills_excluded = sum(
            1 for f in excluded
            if f.get("isTeamKill", False) and not f.get("isSuicide", False))

        frag_count = len(kills)
        rl_kills = sum(1 for f in kills if f.get("weapon") == "rl")
        score = score_run(frag_count, rl_kills)

        crosscheck = {"api_frags": api_frags, "computed": frag_count,
                      "ok": api_frags is None or api_frags == frag_count}
        if not crosscheck["ok"]:
            log.warning(
                "frag-count mismatch for %s quad run @%.3fs: computed %d, "
                "PowerupEvents[].frags %s (sha %s)",
                holder, common.ms_to_s(taken_at_ms), frag_count, api_frags, sha)

        pickup_s = common.ms_to_s(taken_at_ms)
        run_end_s = common.ms_to_s(run_end_ms)
        kill_times_s = [common.ms_to_s(f["time"]) for f in kills]
        start_s, end_s = common.compute_window(
            pickup_s, run_end_s, kill_times_s,
            match_start_s=common.ms_to_s(overview.get("matchStart") or 0),
            match_end_s=common.ms_to_s(match_end_ms) if match_end_ms else None)

        runs.append({
            "holder": holder,
            "team": phase.get("team"),
            "pickup_s": pickup_s,
            "run_end_s": run_end_s,
            "window": {"start_s": start_s, "end_s": end_s},
            "frag_count": frag_count,
            "rl_kills": rl_kills,
            "score": score,
            "kills": kills,
            "suicides_excluded": suicides_excluded,
            "teamkills_excluded": teamkills_excluded,
            "crosscheck": crosscheck,
        })

    # Best first: score, then raw frag count, then earlier pickup.
    runs.sort(key=lambda r: (-r["score"], -r["frag_count"], r["pickup_s"]))

    candidates = []
    for rank, run in enumerate(runs, start=1):
        if run["frag_count"] < min_frags:
            continue
        userids = meta["player_userids"]
        description = (
            f"{run['holder']}'s quad run on {meta['map']} at "
            f"{_mmss(run['pickup_s'])}: {run['frag_count']} frags"
            f" ({run['rl_kills']} RL)."
        )  # plain stub — issue #10 builds the real describer
        candidates.append({
            "category": CATEGORY,
            "demo_sha": sha,
            "map": meta["map"],
            "matchdate": meta["matchdate"],
            "teams": meta["teams"],
            "pov_player": {"name": run["holder"],
                           "userid": userids.get(run["holder"])},
            "window": run["window"],
            "score": run["score"],
            "metrics": {
                "quad_pickup_s": run["pickup_s"],
                "quad_end_s": run["run_end_s"],
                "quad_duration_s": round(run["run_end_s"] - run["pickup_s"], 3),
                "frags_in_run": run["frag_count"],
                "rl_kills": run["rl_kills"],
                "suicides_excluded": run["suicides_excluded"],
                "teamkills_excluded": run["teamkills_excluded"],
                "kills": [
                    {
                        "t_s": common.ms_to_s(f["time"]),
                        "weapon": f.get("weapon"),
                        "victim": f.get("victim"),
                        "teamkill": f.get("isTeamKill", False),
                    }
                    for f in run["kills"]
                ],
                "crosscheck": run["crosscheck"],
                "rank": rank,
                "total_quad_runs": len(runs),
            },
            "description": description,
            "hub_url": "",  # placeholder — issue #9 resolves hub deep links
        })
    return candidates


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipsmith.detectors.quad_runner",
        description="QUAD RUNNER OF THE WEEK detector (spec category 1)",
    )
    parser.add_argument("--sha", required=True,
                        help="64-char demo sha256 (mvd-api sha:<hex> id)")
    parser.add_argument("--api-url", default=None,
                        help=f"mvd-api base URL (default {common.DEFAULT_API_URL})")
    parser.add_argument("--db",
                        help="store DB path (default: state/candidates.db, or $CLIPSMITH_DB)")
    parser.add_argument("--limit", type=int, default=3,
                        help="emit at most N top-scored candidates (default 3; spec max per category)")
    parser.add_argument("--min-frags", type=int, default=1,
                        help="skip runs with fewer scoring frags (default 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print candidate JSON without writing to the store")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = common.MvdApiClient(args.api_url or common.DEFAULT_API_URL)
    candidates = detect(client, args.sha, min_frags=args.min_frags)
    if not candidates:
        print("no qualifying quad runs", file=sys.stderr)
        return

    for cand in candidates[: args.limit]:
        cand_id = common.emit_candidate(cand, db_path=args.db,
                                        dry_run=args.dry_run)
        if cand_id is not None:
            print(cand_id)


if __name__ == "__main__":
    main()
