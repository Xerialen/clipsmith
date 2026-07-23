"""TEAM KILLER OF THE WEEK detector (pipeline-spec.md category 2, issue #6).

Given a demo sha, collects the demo's teamkills per killer, clusters them
into incidents, tests each incident against the spec criteria, and emits
candidates into the candidate store.

Criteria (either qualifies an incident)
    (a) 1+ teammates killed with RL or LG within a 30 s window, or
    (b) 2-4 teammates killed within 0-20 s regardless of weapon.
    The spec phrases (b) as "2-4"; incidents with MORE than 4 teamkills in
    20 s necessarily contain a qualifying 2-4 subset, so they qualify too.

Data surfaces (two-tier)
    Primary: getFrags entries with ``isTeamKill: true`` (full fidelity —
    time, weapon, victim per kill). This is the surface named by the spec.

    Fallback: the mvd-api build in production (schemaVersion 12) emits
    ZERO ``isTeamKill`` frags for every demo probed (15/15), while
    getDemoInfo ``stats.tk`` reports 8-32 teamkills per 4on4 demo —
    QuakeWorld teamkill obituaries ("X squished a teammate", ...) do not
    name the victim, so the kill-log parser drops them. When the kill log
    carries no teamkills but ``stats.tk`` shows some, teamkills are
    reconstructed from getEvents: each ``frag`` event with a negative
    score delta that is not explained by a kill-log suicide at ~the same
    time is one teamkill penalty; the victim is the same-team death event
    within ``DEATH_JOIN_TOL_S`` (null when ambiguous); the weapon is
    unrecoverable (null). Reconstructed incidents therefore can only
    qualify via criterion (b). ``metrics.source`` records which tier
    produced the candidate.

Clustering
    Per killer, teamkills sorted by time are split into incidents wherever
    the gap to the previous teamkill exceeds ``CLUSTER_GAP_S`` (20 s — the
    criterion-(b) span, comfortably inside the 30 s render window).

Scoring formula (more TKs in less time ranks higher)
    ``score = tk_count
              + WEAPON_BONUS_PER_KILL * rl_lg_tks
              + (COMPACT_BONUS_MAX * max(0, 1 - span_s / BURST_SPAN_S)
                 if tk_count >= 2 else 0)``

    - ``tk_count`` dominates: with WEAPON_BONUS_PER_KILL = 0.15 and
      COMPACT_BONUS_MAX = 0.25 the combined bonus stays below 1.0 for any
      incident of up to 4 teamkills (criterion (b)'s range), so an extra
      teamkill always outranks any amount of style/compactness.
    - RL/LG teamkills carry the spec's weapon severity (criterion (a)).
    - ``span_s`` = last TK - first TK; a tight burst outranks a sprawl of
      the same size. Single-TK incidents get no compactness bonus.
    - Ties break on raw tk_count (desc), then earlier first kill.

Window
    ~30 s centred on the incident (midpoint of first/last teamkill),
    clamped to match bounds; the 30 s length is preserved by shifting when
    one edge clamps (pure function :func:`compute_incident_window`).

Reconciliation
    Detected teamkill totals per player are compared against getDemoInfo
    ``stats.tk`` when available; mismatches log a WARNING, never fail
    (reconstruction systematically undercounts: simultaneous gain/penalty
    score updates net out in the frag-event deltas).

CLI
    python -m clipsmith.detectors.team_killer --sha <sha> [--db PATH]
        [--api-url URL] [--limit N] [--min-tks N] [--dry-run]

    --min-tks N is the relaxed/validation mode: ALSO emit incidents with
    >= N teamkills even when neither spec criterion fires (marked
    ``metrics.criteria`` all-false). Default: spec criteria only.
    --dry-run prints candidate JSON without writing to the store.
"""
import argparse
import logging
import sys
import urllib.error

from clipsmith.detectors import common

log = logging.getLogger("clipsmith.detectors.team_killer")

CATEGORY = "TEAM KILLER"

RL_LG_WEAPONS = frozenset({"rl", "lg"})
CLUSTER_GAP_S = 20.0        # split incidents on gaps > this
BURST_SPAN_S = 20.0         # criterion (b): 2+ TKs within this span
WINDOW_LEN_S = 30.0         # rendered window length
WEAPON_BONUS_PER_KILL = 0.15  # see scoring formula in module docstring
COMPACT_BONUS_MAX = 0.25      # see scoring formula in module docstring
SUICIDE_JOIN_TOL_S = 1.0    # frag-delta penalty <-> kill-log suicide join
DEATH_JOIN_TOL_S = 0.8      # frag-delta penalty <-> teammate death join

SOURCE_FRAG_LOG = "frag_log"
SOURCE_RECONSTRUCTION = "frag_delta_reconstruction"


# ----------------------------------------------------------- pure helpers

def score_incident(tk_count, rl_lg_tks, span_s):
    """Score per the module-docstring formula. Pure."""
    score = float(tk_count) + WEAPON_BONUS_PER_KILL * rl_lg_tks
    if tk_count >= 2:
        score += COMPACT_BONUS_MAX * max(0.0, 1.0 - span_s / BURST_SPAN_S)
    return score


def cluster_teamkills(kills, gap_s=CLUSTER_GAP_S):
    """Split a killer's teamkills (dicts with ``t_s``) into incidents.

    Gap-based: a new incident starts whenever the time since the previous
    teamkill exceeds ``gap_s``. Returns a list of lists, time-ordered.
    Pure.
    """
    clusters = []
    for k in sorted(kills, key=lambda k: k["t_s"]):
        if clusters and k["t_s"] - clusters[-1][-1]["t_s"] <= gap_s:
            clusters[-1].append(k)
        else:
            clusters.append([k])
    return clusters


def incident_criteria(kills):
    """Test one incident against the two spec criteria. Pure.

    Returns ``{"rl_lg_30s": bool, "burst_20s": bool}``:
    - ``rl_lg_30s``: >= 1 RL/LG teamkill (the cluster sits inside a 30 s
      window by construction: CLUSTER_GAP_S splits sprawls).
    - ``burst_20s``: >= 2 teamkills within any ``BURST_SPAN_S`` sliding
      window (spec says 2-4; bigger bursts contain a qualifying subset).
    """
    times = sorted(k["t_s"] for k in kills)
    burst = any(
        sum(1 for t in times if t0 <= t <= t0 + BURST_SPAN_S) >= 2
        for t0 in times
    )
    rl_lg = any(k.get("weapon") in RL_LG_WEAPONS for k in kills)
    return {"rl_lg_30s": rl_lg, "burst_20s": burst}


def compute_incident_window(first_s, last_s, match_start_s=0.0,
                            match_end_s=None, length_s=WINDOW_LEN_S):
    """~``length_s`` window centred on the incident, clamped to match
    bounds. The length is preserved by shifting when one edge clamps
    (shrinks only when the match itself is shorter). Pure.
    """
    centre = (first_s + last_s) / 2.0
    start = centre - length_s / 2.0
    end = start + length_s
    if start < match_start_s:
        start = match_start_s
        end = start + length_s
    if match_end_s is not None and end > match_end_s:
        end = match_end_s
        start = max(match_start_s, end - length_s)
    return start, end


# ------------------------------------------------------ teamkill extraction

def teamkills_from_frag_log(frags):
    """Per-killer teamkills from getFrags ``isTeamKill`` entries.

    Returns ``{killer: [{t_s, weapon, victim}, ...]}`` (time-ordered).
    """
    out = {}
    for f in sorted(frags, key=lambda f: f["time"]):
        if not f.get("isTeamKill", False) or f.get("isSuicide", False):
            continue
        out.setdefault(f["killer"], []).append({
            "t_s": common.ms_to_s(f["time"]),
            "weapon": f.get("weapon"),
            "victim": f.get("victim"),
        })
    return out


def reconstruct_teamkills(frags, events, team_of):
    """Fallback teamkill reconstruction (see module docstring).

    ``frags``: getFrags kill log (for suicide subtraction, MILLISECONDS).
    ``events``: getEvents list (``frag``/``death`` types, SECONDS).
    ``team_of``: player name -> team (from getDemoInfo).

    Returns ``{killer: [{t_s, weapon: None, victim}, ...]}``; ``victim``
    is the unique same-team death within DEATH_JOIN_TOL_S, else None.
    """
    suicide_times = {}
    for f in frags:
        if f.get("isSuicide", False):
            suicide_times.setdefault(f["killer"], []).append(
                common.ms_to_s(f["time"]))

    deaths = [e for e in events if e.get("type") == "death"]

    out = {}
    for e in events:
        if e.get("type") != "frag":
            continue
        delta = (e.get("detail") or {}).get("delta", 0)
        if delta >= 0:
            continue
        killer = e.get("player")
        t = e["t"]
        penalties = -delta
        penalties -= sum(
            1 for st in suicide_times.get(killer, [])
            if abs(st - t) < SUICIDE_JOIN_TOL_S)
        if penalties <= 0:
            continue
        victims = [
            d["player"] for d in deaths
            if abs(d["t"] - t) < DEATH_JOIN_TOL_S
            and d["player"] != killer
            and team_of.get(d["player"]) is not None
            and team_of.get(d["player"]) == team_of.get(killer)
        ]
        victim = victims[0] if len(victims) == 1 else None
        for _ in range(penalties):
            out.setdefault(killer, []).append(
                {"t_s": t, "weapon": None, "victim": victim})
    for kills in out.values():
        kills.sort(key=lambda k: k["t_s"])
    return out


# --------------------------------------------------------------- detection

def _mmss(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _get_demoinfo(client, sha):
    """getDemoInfo, or None when the demo has no KTX demoinfo blob.

    TODO(lead): promote a ``get_demoinfo`` wrapper to common.MvdApiClient
    alongside get_overview/get_frags (endpoint: GET /v1/demos/{id}/demoinfo).
    """
    try:
        return client.get(sha, "demoinfo")
    except urllib.error.HTTPError as e:
        log.info("no demoinfo for sha %s (HTTP %s) — stats.tk "
                 "reconciliation skipped", sha, e.code)
        return None


def detect(client, sha, min_tks=None):
    """Analyze one demo; return scored TEAM KILLER candidates, best first.

    ``min_tks``: relaxed mode — also emit incidents with >= min_tks
    teamkills even when neither spec criterion fires (None = spec criteria
    only). Candidates follow the store contract minus id/detected_at/status.
    """
    client.load_demo(sha)
    overview = client.get_overview(sha)
    metadata = client.get_metadata(sha)
    meta = common.demo_meta(overview, metadata)
    demoinfo = _get_demoinfo(client, sha)

    frags = client.get_frags(sha).get("frags") or []

    by_killer = teamkills_from_frag_log(frags)
    source = SOURCE_FRAG_LOG

    stats_tk = {}
    if demoinfo:
        stats_tk = {p["name"]: p.get("stats", {}).get("tk") or 0
                    for p in demoinfo.get("players") or []}

    if not by_killer and any(stats_tk.values()):
        # Kill log carries no teamkills but the KTX scoreboard says there
        # were some — reconstruct from frag-delta penalties (see docstring).
        log.warning(
            "kill log has no isTeamKill frags but stats.tk totals %d — "
            "falling back to frag-delta reconstruction (weapons unknown, "
            "criterion (a) cannot fire) (sha %s)",
            sum(stats_tk.values()), sha)
        team_of = {p["name"]: p.get("team")
                   for p in demoinfo.get("players") or []}
        events = client.get_events(sha, types="frag,death").get("events") or []
        by_killer = reconstruct_teamkills(frags, events, team_of)
        source = SOURCE_RECONSTRUCTION

    # ---- stats.tk reconciliation (WARN on mismatch, never fail)
    crosscheck_by_player = {}
    for player in set(by_killer) | {p for p, n in stats_tk.items() if n}:
        computed = len(by_killer.get(player, []))
        api_tk = stats_tk.get(player) if demoinfo else None
        ok = api_tk is None or api_tk == computed
        crosscheck_by_player[player] = {
            "stats_tk": api_tk, "computed": computed, "ok": ok}
        if not ok:
            log.warning(
                "teamkill-count mismatch for %s: computed %d, "
                "demoinfo stats.tk %s (source %s, sha %s)",
                player, computed, api_tk, source, sha)

    match_start_s = common.ms_to_s(overview.get("matchStart") or 0)
    match_end_ms = overview.get("matchEnd") or 0
    match_end_s = common.ms_to_s(match_end_ms) if match_end_ms else None

    incidents = []
    for killer, kills in by_killer.items():
        for cluster in cluster_teamkills(kills):
            criteria = incident_criteria(cluster)
            qualifies = criteria["rl_lg_30s"] or criteria["burst_20s"]
            relaxed_hit = min_tks is not None and len(cluster) >= min_tks
            if not qualifies and not relaxed_hit:
                continue
            times = [k["t_s"] for k in cluster]
            first_s, last_s = min(times), max(times)
            span_s = last_s - first_s
            rl_lg_tks = sum(1 for k in cluster
                            if k.get("weapon") in RL_LG_WEAPONS)
            start_s, end_s = compute_incident_window(
                first_s, last_s, match_start_s, match_end_s)
            incidents.append({
                "killer": killer,
                "kills": cluster,
                "tk_count": len(cluster),
                "rl_lg_tks": rl_lg_tks,
                "first_tk_s": first_s,
                "last_tk_s": last_s,
                "span_s": span_s,
                "criteria": criteria,
                "window": {"start_s": start_s, "end_s": end_s},
                "score": score_incident(len(cluster), rl_lg_tks, span_s),
            })

    # Best first: score, then raw teamkill count, then earlier incident.
    incidents.sort(key=lambda i: (-i["score"], -i["tk_count"],
                                  i["first_tk_s"]))

    candidates = []
    userids = meta["player_userids"]
    for rank, inc in enumerate(incidents, start=1):
        n = inc["tk_count"]
        description = (
            f"{inc['killer']} killed {n} teammate{'s' if n != 1 else ''} "
            f"on {meta['map']} at {_mmss(inc['first_tk_s'])}"
            + (f" within {inc['span_s']:.0f} s" if n > 1 else "")
            + f" ({inc['rl_lg_tks']} with RL/LG)."
        )  # plain stub — issue #10 builds the real describer
        candidates.append({
            "category": CATEGORY,
            "demo_sha": sha,
            "map": meta["map"],
            "matchdate": meta["matchdate"],
            "teams": meta["teams"],
            "pov_player": {"name": inc["killer"],
                           "userid": userids.get(inc["killer"])},
            "window": inc["window"],
            "score": inc["score"],
            "metrics": {
                "tk_count": inc["tk_count"],
                "rl_lg_tks": inc["rl_lg_tks"],
                "first_tk_s": inc["first_tk_s"],
                "last_tk_s": inc["last_tk_s"],
                "span_s": round(inc["span_s"], 3),
                "criteria": inc["criteria"],
                "source": source,
                # teamkills get the "oops" SFX treatment in Phase 3 —
                # every kill here is flagged teamkill=true for that.
                "kills": [
                    {"t_s": k["t_s"], "weapon": k.get("weapon"),
                     "victim": k.get("victim"), "teamkill": True}
                    for k in inc["kills"]
                ],
                "crosscheck": crosscheck_by_player.get(
                    inc["killer"],
                    {"stats_tk": None, "computed": inc["tk_count"],
                     "ok": True}),
                "rank": rank,
                "total_incidents": len(incidents),
            },
            "description": description,
            "hub_url": "",  # placeholder — issue #9 resolves hub deep links
        })
    return candidates


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipsmith.detectors.team_killer",
        description="TEAM KILLER OF THE WEEK detector (spec category 2)",
    )
    parser.add_argument("--sha", required=True,
                        help="64-char demo sha256 (mvd-api sha:<hex> id)")
    parser.add_argument("--api-url", default=None,
                        help=f"mvd-api base URL (default {common.DEFAULT_API_URL})")
    parser.add_argument("--db",
                        help="store DB path (default: state/candidates.db, or $CLIPSMITH_DB)")
    parser.add_argument("--limit", type=int, default=3,
                        help="emit at most N top-scored candidates (default 3; spec max per category)")
    parser.add_argument("--min-tks", type=int, default=None,
                        help="relaxed mode: also emit incidents with >= N teamkills "
                             "even when neither spec criterion fires (default: criteria only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print candidate JSON without writing to the store")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = common.MvdApiClient(args.api_url or common.DEFAULT_API_URL)
    candidates = detect(client, args.sha, min_tks=args.min_tks)
    if not candidates:
        print("no qualifying teamkill incidents", file=sys.stderr)
        return

    for cand in candidates[: args.limit]:
        cand_id = common.emit_candidate(cand, db_path=args.db,
                                        dry_run=args.dry_run)
        if cand_id is not None:
            print(cand_id)


if __name__ == "__main__":
    main()
