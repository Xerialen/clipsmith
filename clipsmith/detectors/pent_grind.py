"""PENT GRIND OF THE WEEK detector (pipeline-spec.md category 3, issue #7).

Given a demo sha, enumerates pent cycles via getItems(items=pent) phases,
counts deaths — both teams, suicides and teamkills included (dying to lava
at pent is part of the grind) — in each cycle's contested interval
``availableFrom -> takenAt``, and emits the bloodiest cycle(s) as
candidates into the candidate store.

Contested interval & death source
    Each ``items=pent`` phase with a taker bounds one contest:
    [``availableFrom``, ``takenAt``] (milliseconds). Deaths come from the
    getFrags kill log (every death — including suicides — is an obituary
    entry there), which carries the killer/victim/weapon/teamkill detail
    the candidate contract wants; the bare ``/events?types=death`` stream
    does not. Phases nobody took (``takenAt`` null) are skipped — the spec
    wants the grind *before someone actually took it*.

    Match-start guard: deaths stamped exactly at ``matchStart`` are
    dropped. The first pent phase opens at match start, and KTX logs the
    countdown-teleport spawn telefrags at t=0 — physically no gameplay
    death can happen in 0 ms, so these are artifacts, not pent fighting
    (observed on both validation dm3 demos).

Location filter (spec's "deaths near the pent location")
    The pent item itself carries its world coordinates (``x``/``y``/``z``)
    and loc name. For each death in a contested interval the victim's
    position at the death time is fetched via ``/state-at`` (``fields=pos``)
    and the death counts only when its euclidean distance to the pent is
    <= ``--radius`` (default ``PENT_PROXIMITY_RADIUS`` = 768 game units,
    roughly the pent room plus its doorways). Loc-*name* matching was
    rejected: ``/loc-graph`` adjacency covers only transitions observed in
    that demo, so it under-approximates "near".

    Documented approximations:
    - If the pent item exposes no coordinates, the detector falls back to
      counting ALL deaths in the contested interval (``location_filter:
      "interval-only"`` in metrics) — acceptable v1 per issue #7 point 2.
    - If an individual victim's position cannot be resolved (state-at
      error or missing ``pos``), that death IS counted and flagged
      (``near_pent: null``): missing data is rare and the time interval
      already bounds relevance; silently dropping deaths would understate
      the grind.

Scoring formula
    ``score = death count in the contested interval`` (location-filtered
    when coordinates are available). Ties are broken by the raw
    in-interval death count (more total carnage during the contest),
    then by shorter contest duration (same blood in less time is a
    fiercer grind), then earlier pickup.

POV
    ``pov_player`` = the player who finally took the pent — the grind is
    their payoff (issue #7 point 4).

Window (pure function :func:`compute_grind_window`, unit-tested offline)
    ~30 s ending shortly after the pickup: ``takenAt − 27 s … takenAt + 3 s``,
    clamped at the early edge to ``availableFrom − 5 s`` when the contest
    was shorter than the window, and to match bounds.

CLI
    python -m clipsmith.detectors.pent_grind --sha <sha> [--db PATH]
        [--api-url URL] [--limit N] [--min-deaths N] [--radius UNITS]
        [--dry-run]

    --dry-run prints candidate JSON without writing to the store.
"""
import argparse
import logging
import math
import sys

from clipsmith.detectors import common

log = logging.getLogger("clipsmith.detectors.pent_grind")

CATEGORY = "PENT GRIND"

PENT_PROXIMITY_RADIUS = 768.0  # game units; ~pent room + doorways
WINDOW_PRE_S = 27.0            # window starts this long before the pickup
WINDOW_POST_S = 3.0            # ... and ends this long after it
SPAWN_LEAD_S = 5.0             # early-edge clamp: availableFrom − 5 s


def compute_grind_window(available_from_s, taken_at_s,
                         pre_s=WINDOW_PRE_S, post_s=WINDOW_POST_S,
                         lead_s=SPAWN_LEAD_S,
                         match_start_s=0.0, match_end_s=None):
    """Pure window math for PENT GRIND (spec category 3).

    Window = ``taken_at − pre_s … taken_at + post_s`` (~30 s ending
    shortly after the pickup). The early edge is clamped to
    ``available_from − lead_s`` when the contest was shorter than the
    window (don't rewind into the previous pent cycle), and to
    ``match_start_s``. The late edge is capped at ``match_end_s`` when
    known. Returns ``(start_s, end_s)``.
    """
    start_s = max(match_start_s, taken_at_s - pre_s, available_from_s - lead_s)
    end_s = taken_at_s + post_s
    if match_end_s is not None:
        end_s = min(end_s, match_end_s)
    return start_s, max(start_s, end_s)


def deaths_in_interval(frags, start_ms, end_ms, match_start_ms=None):
    """All deaths in [start_ms, end_ms] from a getFrags kill log.

    Both teams, suicides AND teamkills included — every obituary entry is
    one death of ``victim``. Deaths stamped exactly at ``match_start_ms``
    are dropped (countdown-teleport spawn telefrags — see module
    docstring). Returns the frag dicts in time order.

    (common.filter_kills is killer-centric and exclusion-flavored, so the
    grind needs its own victim-centric filter.)
    # TODO(lead): promote to common.py if PENT RUNNER / TEAM KILLER want it
    """
    out = [f for f in frags
           if start_ms <= f["time"] <= end_ms
           and f["time"] != match_start_ms]
    out.sort(key=lambda f: f["time"])
    return out


def euclid(pos, xyz):
    """Distance between a state-at ``pos`` dict and an ``(x, y, z)`` tuple."""
    return math.sqrt((pos["x"] - xyz[0]) ** 2
                     + (pos["y"] - xyz[1]) ** 2
                     + (pos["z"] - xyz[2]) ** 2)


def _state_at(client, sha, t_s):
    """GET /state-at at ``t_s`` with positions + loc names.

    Uses the generic ``client.get`` because common.MvdApiClient has no
    named wrapper yet.
    # TODO(lead): promote a get_state_at wrapper to common.MvdApiClient
    """
    return client.get(sha, "state-at", time=round(t_s, 3), fields="pos,li")


def _player_teams(client, sha):
    """name -> team from /demoinfo (KTX scoreboard); {} when unavailable.

    # TODO(lead): promote a get_demoinfo wrapper to common.MvdApiClient
    """
    try:
        info = client.get(sha, "demoinfo")
    except Exception as exc:  # demoinfo is best-effort enrichment only
        log.warning("demoinfo unavailable for sha %s: %s", sha, exc)
        return {}
    return {p["name"]: p.get("team", "")
            for p in (info or {}).get("players") or [] if p.get("name")}


def _locate_death(client, sha, frag, pent_xyz, radius):
    """Resolve one death's position/loc and proximity verdict.

    Returns ``(near, distance, loc)`` where ``near`` is True/False, or
    None when the victim's position could not be resolved (counted, per
    module docstring).
    """
    victim = frag.get("victim")
    try:
        state = _state_at(client, sha, common.ms_to_s(frag["time"]))
    except Exception as exc:
        log.warning("state-at failed at %.3fs for %r: %s",
                    common.ms_to_s(frag["time"]), victim, exc)
        return None, None, None
    pstate = ((state or {}).get("players") or {}).get(victim) or {}
    pos = pstate.get("pos")
    loc = pstate.get("loc")
    if not pos:
        return None, None, loc
    dist = euclid(pos, pent_xyz)
    return dist <= radius, round(dist, 1), loc


def _mmss(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def detect(client, sha, min_deaths=1, radius=PENT_PROXIMITY_RADIUS):
    """Analyze one demo; return scored PENT GRIND candidates, best first.

    Candidates follow the store contract minus id/detected_at/status (the
    store fills those). See module docstring for the scoring formula and
    the location-filter / fallback rules.
    """
    client.load_demo(sha)
    overview = client.get_overview(sha)
    metadata = client.get_metadata(sha)
    meta = common.demo_meta(overview, metadata)
    teams_by_player = _player_teams(client, sha)

    frags = client.get_frags(sha).get("frags") or []
    items = client.get_items(sha, items="pent").get("items") or []

    match_start_ms = overview.get("matchStart") or 0
    match_end_ms = overview.get("matchEnd") or 0

    contests = []
    total_phases = 0
    for item in items:
        if item.get("kind") != "pent" and item.get("name") != "pent":
            continue
        coords = (item.get("x"), item.get("y"), item.get("z"))
        pent_xyz = coords if all(c is not None for c in coords) else None
        pent_loc = item.get("loc") or ""
        if pent_xyz is None:
            log.warning("pent item has no coordinates (sha %s) — "
                        "falling back to interval-only death counting", sha)

        for phase in item.get("phases") or []:
            taken_at_ms = phase.get("takenAt")
            taker = phase.get("takenBy")
            if taken_at_ms is None or not taker:
                continue  # nobody took it — no grind payoff (spec cat. 3)
            total_phases += 1
            available_ms = phase.get("availableFrom") or 0

            deaths = []
            for f in deaths_in_interval(frags, available_ms, taken_at_ms,
                                        match_start_ms=match_start_ms):
                if pent_xyz is not None:
                    near, dist, loc = _locate_death(
                        client, sha, f, pent_xyz, radius)
                else:
                    near, dist, loc = None, None, None
                deaths.append({
                    "t_s": common.ms_to_s(f["time"]),
                    "victim": f.get("victim"),
                    "victim_team": teams_by_player.get(f.get("victim"), ""),
                    "killer": f.get("killer"),
                    "weapon": f.get("weapon"),
                    "teamkill": f.get("isTeamKill", False),
                    "suicide": f.get("isSuicide", False),
                    "near_pent": near,
                    "distance": dist,
                    "loc": loc,
                })

            # near_pent False = excluded; True/None = counted (see docstring)
            counted = [d for d in deaths if d["near_pent"] is not False]
            by_team = {}
            for d in counted:
                team = d["victim_team"] or "?"
                by_team[team] = by_team.get(team, 0) + 1

            available_s = common.ms_to_s(available_ms)
            taken_s = common.ms_to_s(taken_at_ms)
            start_s, end_s = compute_grind_window(
                available_s, taken_s,
                match_start_s=common.ms_to_s(match_start_ms),
                match_end_s=common.ms_to_s(match_end_ms) if match_end_ms else None)

            contests.append({
                "taker": taker,
                "team": phase.get("team"),
                "available_s": available_s,
                "taken_s": taken_s,
                "contest_duration_s": round(taken_s - available_s, 3),
                "window": {"start_s": start_s, "end_s": end_s},
                "score": len(counted),
                "deaths": deaths,
                "deaths_total": len(counted),
                "deaths_in_interval": len(deaths),
                "deaths_by_team": by_team,
                "pent_loc": pent_loc,
                "location_filter": "proximity" if pent_xyz else "interval-only",
            })

    # Bloodiest first; ties: raw in-interval deaths, shorter contest,
    # earlier pickup (see scoring formula in module docstring).
    contests.sort(key=lambda c: (-c["score"], -c["deaths_in_interval"],
                                 c["contest_duration_s"], c["taken_s"]))

    candidates = []
    for rank, c in enumerate(contests, start=1):
        if c["score"] < min_deaths:
            continue
        userids = meta["player_userids"]
        description = (
            f"The pent grind on {meta['map']} at {_mmss(c['taken_s'])}: "
            f"{c['deaths_total']} death(s) in the "
            f"{c['contest_duration_s']:.1f} s fight before "
            f"{c['taker']} took it."
        )  # plain stub — issue #10 builds the real describer
        candidates.append({
            "category": CATEGORY,
            "demo_sha": sha,
            "map": meta["map"],
            "matchdate": meta["matchdate"],
            "teams": meta["teams"],
            "pov_player": {"name": c["taker"],
                           "userid": userids.get(c["taker"])},
            "window": c["window"],
            "score": c["score"],
            "metrics": {
                "pent_available_s": c["available_s"],
                "pent_taken_s": c["taken_s"],
                "contest_duration_s": c["contest_duration_s"],
                "pent_taker": c["taker"],
                "pent_taker_team": c["team"],
                "pent_loc": c["pent_loc"],
                "deaths_total": c["deaths_total"],
                "deaths_in_interval": c["deaths_in_interval"],
                "deaths_by_team": c["deaths_by_team"],
                "deaths": c["deaths"],
                "location_filter": c["location_filter"],
                "proximity_radius": radius,
                "rank": rank,
                "total_pent_phases": total_phases,
            },
            "description": description,
            "hub_url": "",  # placeholder — issue #9 resolves hub deep links
        })
    return candidates


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipsmith.detectors.pent_grind",
        description="PENT GRIND OF THE WEEK detector (spec category 3)",
    )
    parser.add_argument("--sha", required=True,
                        help="64-char demo sha256 (mvd-api sha:<hex> id)")
    parser.add_argument("--api-url", default=None,
                        help=f"mvd-api base URL (default {common.DEFAULT_API_URL})")
    parser.add_argument("--db",
                        help="store DB path (default: state/candidates.db, or $CLIPSMITH_DB)")
    parser.add_argument("--limit", type=int, default=3,
                        help="emit at most N top-scored candidates (default 3; spec max per category)")
    parser.add_argument("--min-deaths", type=int, default=1,
                        help="skip contests with fewer counted deaths (default 1)")
    parser.add_argument("--radius", type=float, default=PENT_PROXIMITY_RADIUS,
                        help="near-pent proximity radius in game units "
                             f"(default {PENT_PROXIMITY_RADIUS:g})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print candidate JSON without writing to the store")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = common.MvdApiClient(args.api_url or common.DEFAULT_API_URL)
    candidates = detect(client, args.sha, min_deaths=args.min_deaths,
                        radius=args.radius)
    if not candidates:
        print("no qualifying pent grinds", file=sys.stderr)
        return

    for cand in candidates[: args.limit]:
        cand_id = common.emit_candidate(cand, db_path=args.db,
                                        dry_run=args.dry_run)
        if cand_id is not None:
            print(cand_id)


if __name__ == "__main__":
    main()
