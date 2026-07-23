"""PENT RUNNER OF THE WEEK detector (pipeline-spec.md category 4, issue #8).

Given a demo sha, enumerates pent runs via getItems(items=pent) phases,
scores each run from the getFrags kill log with RL/LG-carrier weighting,
cross-checks against PowerupEvents[].frags, and emits candidates into the
candidate store.

Run enumeration
    Each ``items=pent`` phase (``takenAt``/``takenBy``) is one run. The held
    interval is [takenAt, expiry]: expiry comes from the matching powerup
    event's ``detail.endTime`` (PowerupEvents — joined on holder name and
    pickup time within ``EVENT_JOIN_TOLERANCE_MS``); if no event matches, it
    falls back to takenAt + 30 s (the pent powerup duration — pent lasts 30 s
    like quad; the phase's ``respawnAt`` is the 5-minute item respawn, NOT
    the expiry), capped at match end. The matched event's ``detail.frags``
    doubles as the cross-check count (WARN on mismatch with the computed
    count, never fail).

    Match-start guard (mirrors pent_grind): kills stamped exactly at
    ``matchStart`` are dropped — KTX logs the countdown-teleport spawn
    telefrags at t=0; they are artifacts, not pent fragging.

Carrier attribution (issue #8 point 4 — method documented per ticket)
    A victim "was carrying RL/LG at death" is resolved from two joined
    sources, union of both:

    - **Primary — /weapon-pickups**: each record is one pickup with
      ``(player, weapon, time, nextDeathTime)``; QW players lose weapons on
      death, so the player held ``weapon`` over [``time``, ``nextDeathTime``]
      (``nextDeathTime`` absent/0 = held to match end). The victim carried W
      at death time T iff a pickup of W by the victim covers T
      (``time <= T <= nextDeathTime + CARRIER_JOIN_TOLERANCE_MS``).
    - **Corroboration — /backpacks**: KTX drops an RL/LG backpack when the
      holder dies; a drop by the victim within
      ``CARRIER_JOIN_TOLERANCE_MS`` of T also marks carrier (the proven
      Phase-0 ``rl_carrier_kills.py`` join).

    RL takes precedence over LG when the victim held both (per-kill flag is
    one of ``"rl"`` / ``"lg"`` / ``"none"``).

    Fallback: if BOTH endpoints are unavailable (fetch error), carrier flags
    become ``None`` (unknown) and the bonus falls back to the kill-weapon
    log — kills made WITH rl/lg are weighted instead — flagged in metrics as
    ``carrier_attribution.method = "kill-weapon-fallback"``.

Scoring formula (issue #8 points 3–4)
    ``score = frags + CARRIER_BONUS_PER_KILL * carrier_kills``

    - ``frags``: kills by the pent holder during the held interval, from
      getFrags, excluding suicides and teamkills (enemy kills only).
    - ``carrier_kills``: those kills whose victim was carrying RL or LG at
      death (see attribution above).
    - ``CARRIER_BONUS_PER_KILL = 0.5``: strong enough that at identical kill
      counts any carrier kill outranks a carrier-less run (the issue's
      measurement: 2 carrier kills must beat 0), yet a run with one more raw
      frag can at worst be tied (6 frags / 2 carriers = 7.0 ties
      7 frags / 0), and ties are broken by raw frag count (desc), so a
      genuinely higher kill count still wins. Then carrier kills (desc),
      then earlier pickup.

Window
    pickup −5 s … +30 s, extended to the end of the run while the holder
    keeps fragging past +30 s — pure function ``common.compute_window``,
    shared with QUAD RUNNER, unit-tested offline.

CLI
    python -m clipsmith.detectors.pent_runner --sha <sha> [--db PATH]
        [--api-url URL] [--limit N] [--min-frags N] [--dry-run]

    --dry-run prints candidate JSON without writing to the store.
"""
import argparse
import logging
import sys

from clipsmith.detectors import common

log = logging.getLogger("clipsmith.detectors.pent_runner")

CATEGORY = "PENT RUNNER"

PENT_DURATION_S = 30.0          # pent powerup lifetime (fallback expiry)
EVENT_JOIN_TOLERANCE_MS = 2000  # phase.takenAt <-> PowerupEvent.t join slack
CARRIER_BONUS_PER_KILL = 0.5    # see scoring formula in module docstring
CARRIER_JOIN_TOLERANCE_MS = 1500  # death <-> backpack-drop/nextDeath slack
CARRIER_WEAPONS = ("rl", "lg")  # order = reporting precedence (rl first)


def score_run(frag_count, carrier_kills):
    """Score per the module-docstring formula. Pure."""
    return frag_count + CARRIER_BONUS_PER_KILL * carrier_kills


def victim_carrier(victim, death_ms, weapon_pickups, backpacks,
                   tolerance_ms=CARRIER_JOIN_TOLERANCE_MS):
    """Was ``victim`` carrying RL/LG at ``death_ms``? Pure.

    Joins the /weapon-pickups held-intervals (primary) with /backpacks
    death-drops (corroboration) per the module docstring. Returns ``"rl"``,
    ``"lg"`` (RL precedence) or ``"none"``.
    """
    held = set()
    for p in weapon_pickups:
        if p.get("player") != victim or p.get("weapon") not in CARRIER_WEAPONS:
            continue
        start = p.get("time", 0)
        end = p.get("nextDeathTime") or float("inf")  # 0/absent = never died
        if start <= death_ms <= end + tolerance_ms:
            held.add(p["weapon"])
    for b in backpacks:
        if b.get("player") != victim or b.get("weapon") not in CARRIER_WEAPONS:
            continue
        if abs(b.get("time", 0) - death_ms) <= tolerance_ms:
            held.add(b["weapon"])
    for w in CARRIER_WEAPONS:
        if w in held:
            return w
    return "none"


def _mmss(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _match_powerup_event(events, holder, taken_at_ms):
    """Find the pent PowerupEvent for a phase (same holder, pickup within
    tolerance). Returns the event dict or None."""
    best = None
    for e in events:
        if e.get("type") != "powerup" or e.get("player") != holder:
            continue
        detail = e.get("detail") or {}
        if detail.get("powerup") != "pent":
            continue
        dt = abs(e["t"] * 1000.0 - taken_at_ms)
        if dt <= EVENT_JOIN_TOLERANCE_MS and (best is None or dt < best[0]):
            best = (dt, e)
    return best[1] if best else None


def _as_list(payload, key):
    """mvd-api list endpoints return a bare JSON array; tolerate a
    ``{key: [...]}`` wrapper too (MCP-style)."""
    if isinstance(payload, list):
        return payload
    return (payload or {}).get(key) or []


def _fetch_carrier_sources(client, sha):
    """Fetch /weapon-pickups + /backpacks; returns (pickups, packs, ok).

    ``ok=False`` means BOTH fetches failed -> kill-weapon fallback mode.
    Uses the generic ``client.get`` because common.MvdApiClient has no named
    wrappers yet.
    # TODO(lead): promote get_weapon_pickups/get_backpacks wrappers to
    # common.MvdApiClient
    """
    pickups, packs, errors = [], [], 0
    try:
        pickups = _as_list(client.get(sha, "weapon-pickups"), "weaponPickups")
    except Exception as exc:
        log.warning("weapon-pickups unavailable for sha %s: %s", sha, exc)
        errors += 1
    try:
        packs = _as_list(client.get(sha, "backpacks"), "backpacks")
    except Exception as exc:
        log.warning("backpacks unavailable for sha %s: %s", sha, exc)
        errors += 1
    return pickups, packs, errors < 2


def detect(client, sha, min_frags=1):
    """Analyze one demo; return scored PENT RUNNER candidates, best first.

    Candidates follow the store contract minus id/detected_at/status (the
    store fills those). Logs a WARNING when the computed frag count differs
    from PowerupEvents[].frags, and when carrier attribution falls back to
    the kill-weapon log.
    """
    client.load_demo(sha)
    overview = client.get_overview(sha)
    metadata = client.get_metadata(sha)
    meta = common.demo_meta(overview, metadata)

    frags = client.get_frags(sha).get("frags") or []
    items = client.get_items(sha, items="pent").get("items") or []
    events = client.get_events(sha, types="powerup").get("events") or []
    weapon_pickups, backpacks, attribution_ok = _fetch_carrier_sources(
        client, sha)
    if not attribution_ok:
        log.warning("carrier attribution unavailable for sha %s — falling "
                    "back to kill-weapon weighting", sha)

    match_start_ms = overview.get("matchStart") or 0
    match_end_ms = overview.get("matchEnd") or 0

    phases = []
    for item in items:
        if item.get("kind") != "pent" and item.get("name") != "pent":
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
            run_end_ms = taken_at_ms + PENT_DURATION_S * 1000.0
            if match_end_ms:
                run_end_ms = min(run_end_ms, match_end_ms)
            api_frags = None

        kills = [f for f in common.filter_kills(frags, holder, taken_at_ms,
                                                run_end_ms)
                 if f["time"] != match_start_ms]  # spawn-telefrag guard
        excluded = [f for f in common.filter_kills(
            frags, holder, taken_at_ms, run_end_ms,
            include_teamkills=True, include_suicides=True) if f not in kills]
        suicides_excluded = sum(1 for f in excluded if f.get("isSuicide", False))
        teamkills_excluded = sum(
            1 for f in excluded
            if f.get("isTeamKill", False) and not f.get("isSuicide", False))

        kill_rows = []
        for f in kills:
            if attribution_ok:
                carrier = victim_carrier(f.get("victim"), f["time"],
                                         weapon_pickups, backpacks)
            else:
                carrier = None  # unknown — kill-weapon fallback (docstring)
            kill_rows.append({
                "t_s": common.ms_to_s(f["time"]),
                "weapon": f.get("weapon"),
                "victim": f.get("victim"),
                "teamkill": f.get("isTeamKill", False),
                "victim_carrier": carrier,
            })

        frag_count = len(kill_rows)
        if attribution_ok:
            carrier_kills = sum(1 for k in kill_rows
                                if k["victim_carrier"] in CARRIER_WEAPONS)
        else:
            carrier_kills = sum(1 for k in kill_rows
                                if k["weapon"] in CARRIER_WEAPONS)
        rl_carrier_kills = sum(1 for k in kill_rows
                               if k["victim_carrier"] == "rl")
        lg_carrier_kills = sum(1 for k in kill_rows
                               if k["victim_carrier"] == "lg")
        score = score_run(frag_count, carrier_kills)

        crosscheck = {"api_frags": api_frags, "computed": frag_count,
                      "ok": api_frags is None or api_frags == frag_count}
        if not crosscheck["ok"]:
            log.warning(
                "frag-count mismatch for %s pent run @%.3fs: computed %d, "
                "PowerupEvents[].frags %s (sha %s)",
                holder, common.ms_to_s(taken_at_ms), frag_count, api_frags, sha)

        pickup_s = common.ms_to_s(taken_at_ms)
        run_end_s = common.ms_to_s(run_end_ms)
        start_s, end_s = common.compute_window(
            pickup_s, run_end_s, [k["t_s"] for k in kill_rows],
            match_start_s=common.ms_to_s(match_start_ms),
            match_end_s=common.ms_to_s(match_end_ms) if match_end_ms else None)

        runs.append({
            "holder": holder,
            "team": phase.get("team"),
            "pickup_s": pickup_s,
            "run_end_s": run_end_s,
            "window": {"start_s": start_s, "end_s": end_s},
            "frag_count": frag_count,
            "carrier_kills": carrier_kills,
            "rl_carrier_kills": rl_carrier_kills,
            "lg_carrier_kills": lg_carrier_kills,
            "score": score,
            "kills": kill_rows,
            "suicides_excluded": suicides_excluded,
            "teamkills_excluded": teamkills_excluded,
            "crosscheck": crosscheck,
        })

    # Best first: score, then raw frag count, then carrier kills, then
    # earlier pickup (see scoring formula in module docstring).
    runs.sort(key=lambda r: (-r["score"], -r["frag_count"],
                             -r["carrier_kills"], r["pickup_s"]))

    attribution = {
        "method": ("weapon-pickups+backpacks" if attribution_ok
                   else "kill-weapon-fallback"),
        "weapon_pickups": len(weapon_pickups),
        "backpacks": len(backpacks),
        "carrier_bonus_per_kill": CARRIER_BONUS_PER_KILL,
    }

    candidates = []
    for rank, run in enumerate(runs, start=1):
        if run["frag_count"] < min_frags:
            continue
        userids = meta["player_userids"]
        description = (
            f"{run['holder']}'s pent run on {meta['map']} at "
            f"{_mmss(run['pickup_s'])}: {run['frag_count']} frags"
            f" ({run['carrier_kills']} on RL/LG carriers)."
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
                "pent_pickup_s": run["pickup_s"],
                "pent_end_s": run["run_end_s"],
                "pent_duration_s": round(run["run_end_s"] - run["pickup_s"], 3),
                "frags_in_run": run["frag_count"],
                "carrier_kills": run["carrier_kills"],
                "rl_carrier_kills": run["rl_carrier_kills"],
                "lg_carrier_kills": run["lg_carrier_kills"],
                "suicides_excluded": run["suicides_excluded"],
                "teamkills_excluded": run["teamkills_excluded"],
                "kills": run["kills"],
                "carrier_attribution": attribution,
                "crosscheck": run["crosscheck"],
                "rank": rank,
                "total_pent_runs": len(runs),
            },
            "description": description,
            "hub_url": "",  # placeholder — issue #9 resolves hub deep links
        })
    return candidates


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipsmith.detectors.pent_runner",
        description="PENT RUNNER OF THE WEEK detector (spec category 4)",
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
        print("no qualifying pent runs", file=sys.stderr)
        return

    for cand in candidates[: args.limit]:
        cand_id = common.emit_candidate(cand, db_path=args.db,
                                        dry_run=args.dry_run)
        if cand_id is not None:
            print(cand_id)


if __name__ == "__main__":
    main()
