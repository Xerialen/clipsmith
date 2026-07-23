"""COME-BACK OF THE WEEK detector (pipeline-spec.md category 5, issue #30).

Given a demo sha, reconstructs the rolling team-score lead from frag-event
deltas (mvd-api has NO pre-baked score timeline), finds intervals where one
team is behind with the opponent's lead increasing on average for 2+
minutes, locates the point where that trend breaks, joins the break to a
single player killing 2+ enemy RL/LG carriers, and emits candidates into
the candidate store (category ``COMEBACK``).

Score-series reconstruction (issue #30 point 1)
    ``getEvents(types=frag)`` events carry ``t`` (SECONDS), ``player``,
    ``detail.delta`` (+1 kill, negative for suicide/teamkill penalties) and
    ``detail.team`` — folding the deltas per team yields each team's rolling
    frag score; ``lead(t) = score_A(t) - score_B(t)`` is a step function.
    For trend math the step function is resampled onto a uniform grid
    (``GRID_S`` = 1 s samples, value = last step at or before the sample).

Trend definition (issue #30 point 2 — documented choice)
    "Behind with the opponent's lead increasing on average for 2+ minutes"
    is evaluated per team on its **deficit** series
    (``deficit = opponent score - own score``; positive = behind). A grid
    time ``t`` qualifies when over the trailing window ``[t - window_s, t]``
    (``window_s`` = ``WINDOW_S`` = 120 s, the spec's "2+ minutes"):

    1. **behind throughout** — ``deficit > 0`` at every sample in the
       window (a team that ties it up mid-window is not "behind");
    2. **rising on average** — the least-squares linear-fit slope of the
       deficit over the window is >= ``MIN_SLOPE_FPM`` (default 1.0
       frags/minute). A linear fit was chosen over endpoint differencing
       because it weighs the whole window, so a single late frag cannot
       fake a 2-minute trend;
    3. **materially rising** — net deficit increase across the window
       (``deficit(t) - deficit(t - window_s)``) >= ``MIN_NET_INCREASE``
       (default 2 frags), guarding against long flat windows whose fit
       slope is nudged positive by one frag;
    4. **rising throughout** — EACH half of the window contributes
       >= ``HALF_MIN_NET`` (default 1 frag) of deficit growth. This is
       what makes the trend a genuine 2-minute slide: a short 20 s burst
       inside an otherwise flat window satisfies (2) and (3) (the late
       spike drags the whole-window fit positive) but is not "increasing
       on average for 2+ minutes" — the growth must span both minutes.

    Qualifying grid times expand to intervals
    ``[qualifying_t - window_s, qualifying_t]``; overlapping intervals
    merge into one trend (live demos showed a single long slide otherwise
    fragments into several near-duplicate trends whenever a mid-slide
    stall briefly breaks a condition). By construction every reported
    trend spans >= ``window_s``. All parameters are CLI-tunable
    (``--window-s``, ``--min-slope``, ``--min-net-increase``,
    ``--half-min-net``, ``--grid-s``).

Trend break (issue #30 point 3)
    Scanning ``[first_qualifying_t, trend_end + BREAK_SEARCH_S]`` (the
    slide is 2 minutes old from its first qualifying time onward), a break
    is a deficit peak — the last grid time attaining the running maximum —
    from which the deficit then falls by >= ``MIN_RECOVERY`` (default 2
    frags) before making a new high: a material reversal of the losing
    trend, not a one-frag dip. One long slide can contain several material
    reversals (live demos showed dent -> re-worsen -> deeper peak); EACH
    reversal is tested for a breaker, and breaks converging on the same
    play dedup to one candidate (the longest trend behind it wins). A
    trend whose deficit never materially recedes inside the horizon has no
    break (rejected, surfaced in ``--explain`` diagnostics).

Breaker join
    Kills from getFrags (MILLISECONDS) by behind-team players on
    opposing-team victims (suicides/teamkills excluded; kills stamped
    exactly at ``matchStart`` dropped — the Phase-1 KTX countdown-telefrag
    guard), within ``BREAK_JOIN_S`` (default 20 s) of the break point.
    Victim carrier status (RL/LG at death) reuses
    :func:`pent_runner.victim_carrier` — /weapon-pickups held intervals
    corroborated by /backpacks drops; if BOTH endpoints are down the join
    falls back to kills made WITH rl/lg (flagged
    ``carrier_attribution.method = "kill-weapon-fallback"``, mirroring
    PENT RUNNER). A player with >= ``MIN_CARRIER_KILLS`` (default 2)
    carrier kills in the join window is the trend-breaker; the best breaker
    has the most carrier kills, then most total kills in the window, then
    the earliest first carrier kill.

Scoring formula
    ``score = carrier_kills + DEFICIT_WEIGHT * lead_at_worst``

    - ``carrier_kills`` dominates (each = 1.0): the play itself ranks
      first, per the category criteria.
    - ``DEFICIT_WEIGHT = 0.1``: the depth of the hole is the drama
      tiebreaker — a 10-frag deficit adds +1.0, so only a dramatically
      deeper hole can outrank one extra carrier kill.
    - Ties break on deeper ``lead_at_worst``, then earlier break.

Window
    First carrier kill of the breaking play −5 s … +30 s (issue #30
    point 4), clamped to match bounds.

CLI
    python -m clipsmith.detectors.come_back --sha <sha> [--db PATH]
        [--api-url URL] [--limit N] [--dry-run] [--explain]
        [--window-s S] [--min-slope FPM] [--min-net-increase N]
        [--half-min-net N] [--grid-s S] [--break-search-s S]
        [--min-recovery N] [--break-join-s S] [--min-carrier-kills N]

    --dry-run prints candidate JSON without writing to the store.
    --explain prints near-miss diagnostics (trends found and why they
    failed) to stderr — the tuning evidence surface.
"""
import argparse
import json
import logging
import sys

from clipsmith.detectors import common
from clipsmith.detectors.pent_runner import CARRIER_WEAPONS, victim_carrier

log = logging.getLogger("clipsmith.detectors.come_back")

CATEGORY = "COMEBACK"

GRID_S = 1.0              # lead-series resampling step
WINDOW_S = 120.0          # the spec's "2+ minutes" trailing window
MIN_SLOPE_FPM = 1.0       # min linear-fit deficit slope, frags/minute
MIN_NET_INCREASE = 2      # min deficit growth across the window, frags
HALF_MIN_NET = 1          # min deficit growth in EACH half of the window
BREAK_SEARCH_S = 60.0     # how far past a trend to look for its break
MIN_RECOVERY = 2          # deficit drop that makes a reversal material
BREAK_JOIN_S = 20.0       # carrier-kill <-> break-point join slack
MIN_CARRIER_KILLS = 2     # spec: "killing 2+ enemy weapon-carriers"
DEFICIT_WEIGHT = 0.1      # see scoring formula in module docstring
WINDOW_PRE_S = 5.0        # render window: break play -5 s ...
WINDOW_POST_S = 30.0      # ... +30 s


# ----------------------------------------------------------- pure helpers

def build_lead_series(frag_events, team_a, team_b):
    """Fold getEvents ``frag`` deltas into lead breakpoints. Pure.

    ``frag_events``: getEvents list (SECONDS); only ``type == "frag"``
    entries whose ``detail.team`` is ``team_a``/``team_b`` contribute.
    Returns ``[(t_s, lead), ...]`` time-ordered, where
    ``lead = score_a - score_b`` after the event at ``t_s``. The implicit
    starting point is lead 0 before the first event.
    """
    score = {team_a: 0, team_b: 0}
    points = []
    for e in sorted((e for e in frag_events if e.get("type") == "frag"),
                    key=lambda e: e["t"]):
        detail = e.get("detail") or {}
        team = detail.get("team")
        if team not in score:
            continue
        score[team] += detail.get("delta", 0)
        points.append((e["t"], score[team_a] - score[team_b]))
    return points


def sample_step_series(breakpoints, t0_s, t1_s, grid_s=GRID_S):
    """Resample step-function breakpoints onto a uniform grid. Pure.

    Returns ``[(t, value), ...]`` for t = t0_s, t0_s+grid_s, ... <= t1_s;
    value at t is the last breakpoint at or before t (0 before the first).
    Raises ValueError on non-positive ``grid_s`` (the sampling loop would
    never advance).
    """
    if grid_s <= 0:
        raise ValueError(f"grid_s must be > 0, got {grid_s!r}")
    samples = []
    i = 0
    value = 0
    n = len(breakpoints)
    t = t0_s
    while t <= t1_s + 1e-9:
        while i < n and breakpoints[i][0] <= t:
            value = breakpoints[i][1]
            i += 1
        samples.append((t, value))
        t += grid_s
    return samples


def fit_slope(points):
    """Least-squares slope of ``[(x, y), ...]`` in y-units per x-unit.

    Pure; returns 0.0 for degenerate input (< 2 points or zero x-variance).
    """
    n = len(points)
    if n < 2:
        return 0.0
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mean_x) ** 2 for p in points)
    if sxx == 0:
        return 0.0
    sxy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
    return sxy / sxx


def find_losing_trends(deficit_samples, window_s=WINDOW_S,
                       min_slope_fpm=MIN_SLOPE_FPM,
                       min_net_increase=MIN_NET_INCREASE,
                       half_min_net=HALF_MIN_NET):
    """Qualifying "behind and sliding" intervals per the module-docstring
    trend definition (conditions 1-4). Pure.

    ``deficit_samples``: uniform-grid ``[(t_s, deficit), ...]`` (positive
    deficit = behind). Returns a list of trend dicts
    ``{start_s, end_s, duration_s, slope_fpm, net_increase, deficit_start,
    deficit_end, worst_deficit}`` (worst within the interval itself).
    """
    if len(deficit_samples) < 2 or window_s <= 0:
        return []
    grid = deficit_samples[1][0] - deficit_samples[0][0]
    if grid <= 0:
        return []  # degenerate sampling (duplicate/unordered grid times)
    win_n = int(round(window_s / grid))
    if win_n < 1:
        return []
    mid_n = win_n // 2

    qualifying = []
    for i in range(win_n, len(deficit_samples)):
        window = deficit_samples[i - win_n:i + 1]
        if any(v <= 0 for _, v in window):
            continue  # (1) behind throughout
        net = window[-1][1] - window[0][1]
        if net < min_net_increase:
            continue  # (3) materially rising
        if (window[mid_n][1] - window[0][1] < half_min_net
                or window[-1][1] - window[mid_n][1] < half_min_net):
            continue  # (4) rising throughout, not one short burst
        if fit_slope(window) * 60.0 < min_slope_fpm:
            continue  # (2) rising on average
        qualifying.append(i)

    # Merge qualifying times whose trailing windows overlap into one
    # trend: index spans [i - win_n, i] sharing any sample are one slide.
    trends = []
    group = []
    for i in qualifying:
        if group and i - win_n <= group[-1]:
            group.append(i)
        else:
            if group:
                trends.append(_trend_from_group(deficit_samples, group, win_n))
            group = [i]
    if group:
        trends.append(_trend_from_group(deficit_samples, group, win_n))
    return trends


def _trend_from_group(samples, group, win_n):
    """Trend dict for one merged run of qualifying sample indices."""
    first, last = group[0] - win_n, group[-1]
    interval = samples[first:last + 1]
    start_s, deficit_start = interval[0]
    end_s, deficit_end = interval[-1]
    return {
        "start_s": start_s,
        "end_s": end_s,
        "first_qualifying_s": samples[group[0]][0],
        "duration_s": end_s - start_s,
        "slope_fpm": fit_slope(interval) * 60.0,
        "net_increase": deficit_end - deficit_start,
        "deficit_start": deficit_start,
        "deficit_end": deficit_end,
        "worst_deficit": max(v for _, v in interval),
    }


def find_trend_breaks(deficit_samples, from_s, to_s,
                      min_recovery=MIN_RECOVERY):
    """Material reversals of the losing trend in ``[from_s, to_s]``. Pure.

    Zigzag pivot detection with ``min_recovery`` hysteresis: tracks the
    current deficit peak (ties update to the LAST attainment); when the
    deficit falls ``min_recovery`` or more below the peak before exceeding
    it, that peak is one break. A further break requires the deficit to
    first RISE ``min_recovery`` above a subsequent trough (a continuing
    recovery is one reversal, not one break per ``min_recovery`` frags).
    Returns ``[{break_t_s, worst_deficit}, ...]`` time-ordered (empty when
    the deficit never materially recedes — the team just kept losing).
    """
    breaks = []
    peak_t = peak_v = trough_v = None
    armed = True  # the initial rise into the first peak counts
    for t, v in deficit_samples:
        if t < from_s or t > to_s:
            continue
        if armed:
            if peak_v is None or v >= peak_v:
                peak_t, peak_v = t, v
            elif peak_v - v >= min_recovery:
                breaks.append({"break_t_s": peak_t, "worst_deficit": peak_v})
                armed = False
                trough_v = v
        else:
            if v < trough_v:
                trough_v = v
            elif v - trough_v >= min_recovery:
                armed = True
                peak_t, peak_v = t, v
    return breaks


def collect_break_kills(frags, team_of, behind_team, opponent_team,
                        break_t_s, join_s=BREAK_JOIN_S, match_start_ms=0,
                        weapon_pickups=None, backpacks=None,
                        attribution_ok=True):
    """Behind-team kills on enemies around the break point. Pure.

    ``frags``: getFrags kill log (MILLISECONDS). Returns time-ordered kill
    rows ``{t_s, killer, victim, weapon, victim_carrier, is_carrier_kill}``
    for kills by ``behind_team`` players on ``opponent_team`` victims with
    ``|t_s - break_t_s| <= join_s``, excluding suicides/teamkills and kills
    stamped exactly at ``match_start_ms`` (KTX countdown spawn-telefrag
    guard, Phase-1 finding). In fallback mode (``attribution_ok=False``)
    ``victim_carrier`` is None and ``is_carrier_kill`` keys off the kill
    weapon instead.
    """
    rows = []
    for f in frags:
        if f.get("isSuicide", False) or f.get("isTeamKill", False):
            continue
        if f["time"] == match_start_ms:
            continue
        if team_of.get(f.get("killer")) != behind_team:
            continue
        if team_of.get(f.get("victim")) != opponent_team:
            continue
        t_s = common.ms_to_s(f["time"])
        if abs(t_s - break_t_s) > join_s:
            continue
        if attribution_ok:
            carrier = victim_carrier(f.get("victim"), f["time"],
                                     weapon_pickups or [], backpacks or [])
            is_carrier = carrier in CARRIER_WEAPONS
        else:
            carrier = None
            is_carrier = f.get("weapon") in CARRIER_WEAPONS
        rows.append({
            "t_s": t_s,
            "killer": f.get("killer"),
            "victim": f.get("victim"),
            "weapon": f.get("weapon"),
            "victim_carrier": carrier,
            "is_carrier_kill": is_carrier,
        })
    rows.sort(key=lambda r: r["t_s"])
    return rows


def find_breaker(kill_rows, min_carrier_kills=MIN_CARRIER_KILLS):
    """The trend-breaking player from break-window kill rows. Pure.

    Returns ``{"breaker": best_or_None, "players": [...]}``; ``players``
    summarizes every behind-team player active in the window (best first)
    so callers can surface nearest-miss diagnostics. Best = most carrier
    kills, then most total kills, then earliest first carrier kill;
    ``breaker`` is None when nobody reaches ``min_carrier_kills``.
    """
    by_player = {}
    for r in kill_rows:
        s = by_player.setdefault(r["killer"], {
            "player": r["killer"], "carrier_kills": 0, "total_kills": 0,
            "first_carrier_t_s": None, "kills": []})
        s["total_kills"] += 1
        s["kills"].append(r)
        if r["is_carrier_kill"]:
            s["carrier_kills"] += 1
            if s["first_carrier_t_s"] is None:
                s["first_carrier_t_s"] = r["t_s"]
    players = sorted(
        by_player.values(),
        key=lambda s: (-s["carrier_kills"], -s["total_kills"],
                       s["first_carrier_t_s"] if s["first_carrier_t_s"]
                       is not None else float("inf")))
    breaker = (players[0] if players
               and players[0]["carrier_kills"] >= min_carrier_kills
               else None)
    return {"breaker": breaker, "players": players}


def score_comeback(carrier_kills, lead_at_worst):
    """Score per the module-docstring formula. Pure."""
    return carrier_kills + DEFICIT_WEIGHT * lead_at_worst


def compute_break_window(anchor_s, match_start_s=0.0, match_end_s=None,
                         pre_s=WINDOW_PRE_S, post_s=WINDOW_POST_S):
    """Render window for the trend-break play (−5 s … +30 s around the
    first carrier kill), clamped to match bounds. Pure."""
    start = max(match_start_s, anchor_s - pre_s)
    end = anchor_s + post_s
    if match_end_s is not None:
        end = min(end, match_end_s)
    return start, end


# --------------------------------------------------------------- detection

def _mmss(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _fetch_carrier_sources(client, sha):
    """Fetch /weapon-pickups + /backpacks; returns (pickups, packs, ok).

    ``ok=False`` means BOTH fetches failed -> kill-weapon fallback mode
    (same contract as pent_runner's fetch — kept local so failures log to
    this detector's logger).
    """
    pickups, packs, errors = [], [], 0
    try:
        payload = client.get(sha, "weapon-pickups")
        pickups = payload if isinstance(payload, list) else \
            (payload or {}).get("weaponPickups") or []
    except Exception as exc:
        log.warning("weapon-pickups unavailable for sha %s: %s", sha, exc)
        errors += 1
    try:
        payload = client.get(sha, "backpacks")
        packs = payload if isinstance(payload, list) else \
            (payload or {}).get("backpacks") or []
    except Exception as exc:
        log.warning("backpacks unavailable for sha %s: %s", sha, exc)
        errors += 1
    return pickups, packs, errors < 2


def _describe(breaker, behind, opponent, meta, cand_metrics, score):
    """Digest-quality description (what / POV / why) — written here in
    full because the Phase-1 describer has no COMEBACK renderer yet (it
    would fall back to its generic template)."""
    m = cand_metrics
    victims = ", ".join(
        f"{k['victim']} ({(k['victim_carrier'] or k['weapon'] or '?').upper()})"
        for k in breaker["kills"] if k["is_carrier_kill"])
    what = (
        f"{breaker['player']} breaks {behind}'s slide on {meta['map']} at "
        f"{_mmss(m['break_t_s'])}: {opponent} had stretched the lead to "
        f"{m['lead_at_worst']} frags over {m['trend_to_break_s'] / 60.0:.1f} "
        f"minutes, then {breaker['player']} kills "
        f"{m['carrier_kills']} enemy weapon-carriers ({victims}) and turns "
        f"the trend."
    )
    teams = meta.get("teams") or []
    bits = []
    if len(teams) >= 2:
        bits.append(f"{teams[0]['name']} vs {teams[1]['name']} "
                    f"({teams[0]['score']}-{teams[1]['score']})")
    if meta.get("matchdate"):
        bits.append(meta["matchdate"])
    pov = (f"POV: {breaker['player']} ({behind})"
           + (f" — {', '.join(bits)}" if bits else "") + ".")
    why = (f"Why: {opponent}'s lead grew +{m['trend_slope_fpm']:.1f} "
           f"frags/min for {m['trend_to_break_s']:.0f} s before the break; "
           f"{m['carrier_kills']} RL/LG-carrier kills in the breaking play; "
           f"score {score:g}.")
    return f"{what} {pov} {why}"


def analyze(client, sha, window_s=WINDOW_S, min_slope_fpm=MIN_SLOPE_FPM,
            min_net_increase=MIN_NET_INCREASE, half_min_net=HALF_MIN_NET,
            grid_s=GRID_S, break_search_s=BREAK_SEARCH_S,
            min_recovery=MIN_RECOVERY, break_join_s=BREAK_JOIN_S,
            min_carrier_kills=MIN_CARRIER_KILLS):
    """Full comeback analysis for one demo.

    Returns ``{"candidates": [...], "rejections": [...], "trends_found": N}``
    — ``rejections`` carries the nearest-miss diagnostics (each failed
    trend with its stage and detail) so threshold tuning is evidenced.

    Raises ValueError on non-positive ``grid_s`` / ``window_s`` (a zero
    grid would divide by zero in find_losing_trends and hang
    sample_step_series; a zero window makes the trend definition vacuous).
    """
    if grid_s <= 0:
        raise ValueError(f"grid_s must be > 0, got {grid_s!r}")
    if window_s <= 0:
        raise ValueError(f"window_s must be > 0, got {window_s!r}")
    client.load_demo(sha)
    overview = client.get_overview(sha)
    metadata = client.get_metadata(sha)
    meta = common.demo_meta(overview, metadata)

    result = {"candidates": [], "rejections": [], "trends_found": 0}

    team_names = [t["name"] for t in overview.get("teams") or []]
    if len(team_names) != 2:
        log.warning("expected exactly 2 teams, got %r (sha %s) — "
                    "COMEBACK needs a two-team game", team_names, sha)
        return result
    team_a, team_b = team_names

    events = client.get_events(sha, types="frag").get("events") or []
    breakpoints = build_lead_series(events, team_a, team_b)

    match_start_ms = overview.get("matchStart") or 0
    match_end_ms = overview.get("matchEnd") or 0
    match_start_s = common.ms_to_s(match_start_ms)
    match_end_s = (common.ms_to_s(match_end_ms) if match_end_ms
                   else (breakpoints[-1][0] if breakpoints else 0.0))
    lead_samples = sample_step_series(breakpoints, match_start_s,
                                      match_end_s, grid_s)

    frags = client.get_frags(sha).get("frags") or []
    weapon_pickups, backpacks, attribution_ok = _fetch_carrier_sources(
        client, sha)
    if not attribution_ok:
        log.warning("carrier attribution unavailable for sha %s — falling "
                    "back to kill-weapon matching", sha)
    team_of = {p["name"]: p.get("team")
               for p in overview.get("players") or []}

    params = {
        "window_s": window_s, "min_slope_fpm": min_slope_fpm,
        "min_net_increase": min_net_increase, "half_min_net": half_min_net,
        "grid_s": grid_s, "break_search_s": break_search_s,
        "min_recovery": min_recovery, "break_join_s": break_join_s,
        "min_carrier_kills": min_carrier_kills,
    }
    attribution = {
        "method": ("weapon-pickups+backpacks" if attribution_ok
                   else "kill-weapon-fallback"),
        "weapon_pickups": len(weapon_pickups),
        "backpacks": len(backpacks),
    }

    raw = []
    for behind, opponent, sign in ((team_a, team_b, -1), (team_b, team_a, 1)):
        deficit = [(t, sign * v) for t, v in lead_samples]
        trends = find_losing_trends(deficit, window_s=window_s,
                                    min_slope_fpm=min_slope_fpm,
                                    min_net_increase=min_net_increase,
                                    half_min_net=half_min_net)
        result["trends_found"] += len(trends)
        for trend in trends:
            base = {"behind_team": behind, "opponent_team": opponent,
                    "trend": trend}
            breaks = find_trend_breaks(
                deficit, trend["first_qualifying_s"],
                trend["end_s"] + break_search_s, min_recovery=min_recovery)
            if not breaks:
                result["rejections"].append({
                    **base, "stage": "no_break",
                    "detail": f"deficit never receded {min_recovery}+ "
                              f"frags within {break_search_s:.0f} s past "
                              f"the trend — {behind} kept losing"})
                continue
            for brk in breaks:
                kill_rows = collect_break_kills(
                    frags, team_of, behind, opponent, brk["break_t_s"],
                    join_s=break_join_s, match_start_ms=match_start_ms,
                    weapon_pickups=weapon_pickups, backpacks=backpacks,
                    attribution_ok=attribution_ok)
                join = find_breaker(kill_rows,
                                    min_carrier_kills=min_carrier_kills)
                if join["breaker"] is None:
                    nearest = [{k: p[k] for k in
                                ("player", "carrier_kills", "total_kills")}
                               for p in join["players"][:3]]
                    result["rejections"].append({
                        **base, "stage": "no_breaker", "break": brk,
                        "detail": f"no single {behind} player reached "
                                  f"{min_carrier_kills} enemy-carrier kills "
                                  f"within {break_join_s:.0f} s of the break",
                        "nearest_players": nearest})
                    continue
                raw.append({"behind": behind, "opponent": opponent,
                            "trend": trend, "brk": brk,
                            "breaker": join["breaker"]})

    # Distinct trends can still converge on the same break play (their
    # break-search horizons overlap) — one play, one candidate: keep the
    # longest trend behind it.
    deduped = {}
    for r in raw:
        key = (r["behind"], r["breaker"]["player"], r["brk"]["break_t_s"])
        cur = deduped.get(key)
        if cur is None or r["trend"]["duration_s"] > cur["trend"]["duration_s"]:
            deduped[key] = r
    raw = list(deduped.values())

    raw.sort(key=lambda r: (
        -score_comeback(r["breaker"]["carrier_kills"],
                        r["brk"]["worst_deficit"]),
        -r["brk"]["worst_deficit"], r["brk"]["break_t_s"]))

    userids = meta["player_userids"]
    for rank, r in enumerate(raw, start=1):
        breaker, trend, brk = r["breaker"], r["trend"], r["brk"]
        carrier_kill_rows = [k for k in breaker["kills"]
                             if k["is_carrier_kill"]]
        anchor_s = breaker["first_carrier_t_s"]
        start_s, end_s = compute_break_window(
            anchor_s, match_start_s=match_start_s,
            match_end_s=common.ms_to_s(match_end_ms) if match_end_ms
            else None)
        score = score_comeback(breaker["carrier_kills"],
                               brk["worst_deficit"])
        metrics = {
            "behind_team": r["behind"],
            "opponent_team": r["opponent"],
            "trend_start_s": trend["start_s"],
            "trend_end_s": trend["end_s"],
            "trend_duration_s": round(trend["duration_s"], 3),
            "trend_slope_fpm": round(trend["slope_fpm"], 3),
            "trend_net_increase": trend["net_increase"],
            "lead_at_worst": brk["worst_deficit"],
            "break_t_s": brk["break_t_s"],
            "trend_to_break_s": round(brk["break_t_s"] - trend["start_s"], 3),
            "carrier_kills": breaker["carrier_kills"],
            "rl_carrier_kills": sum(1 for k in carrier_kill_rows
                                    if k["victim_carrier"] == "rl"),
            "lg_carrier_kills": sum(1 for k in carrier_kill_rows
                                    if k["victim_carrier"] == "lg"),
            "kills_in_play": breaker["total_kills"],
            "kills": [
                {"t_s": k["t_s"], "weapon": k["weapon"],
                 "victim": k["victim"],
                 "victim_carrier": k["victim_carrier"],
                 "teamkill": False}
                for k in breaker["kills"]
            ],
            "carrier_attribution": attribution,
            "params": params,
            "rank": rank,
            "total_trends": result["trends_found"],
        }
        result["candidates"].append({
            "category": CATEGORY,
            "demo_sha": sha,
            "map": meta["map"],
            "matchdate": meta["matchdate"],
            "teams": meta["teams"],
            "pov_player": {"name": breaker["player"],
                           "userid": userids.get(breaker["player"])},
            "window": {"start_s": start_s, "end_s": end_s},
            "score": score,
            "metrics": metrics,
            "description": _describe(breaker, r["behind"], r["opponent"],
                                     meta, metrics, score),
            "hub_url": "",  # filled by the hub-link resolver (issue #9)
        })
    return result


def detect(client, sha, **params):
    """Analyze one demo; return scored COMEBACK candidates, best first.

    Thin wrapper over :func:`analyze` keeping the detector-family
    interface; ``params`` forward to analyze (window_s, min_slope_fpm,
    min_net_increase, half_min_net, grid_s, break_search_s, min_recovery,
    break_join_s, min_carrier_kills).
    """
    return analyze(client, sha, **params)["candidates"]


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipsmith.detectors.come_back",
        description="COME-BACK OF THE WEEK detector (spec category 5)",
    )
    parser.add_argument("--sha", required=True,
                        help="64-char demo sha256 (mvd-api sha:<hex> id)")
    parser.add_argument("--api-url", default=None,
                        help=f"mvd-api base URL (default {common.DEFAULT_API_URL})")
    parser.add_argument("--db",
                        help="store DB path (default: state/candidates.db, or $CLIPSMITH_DB)")
    parser.add_argument("--limit", type=int, default=3,
                        help="emit at most N top-scored candidates (default 3; spec max per category)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print candidate JSON without writing to the store")
    parser.add_argument("--explain", action="store_true",
                        help="print near-miss diagnostics (failed trends and "
                             "why) to stderr")
    parser.add_argument("--window-s", type=float, default=WINDOW_S,
                        help=f"trend window seconds (default {WINDOW_S:g})")
    parser.add_argument("--min-slope", type=float, default=MIN_SLOPE_FPM,
                        help="min deficit slope, frags/min "
                             f"(default {MIN_SLOPE_FPM:g})")
    parser.add_argument("--min-net-increase", type=float,
                        default=MIN_NET_INCREASE,
                        help="min deficit growth across the window, frags "
                             f"(default {MIN_NET_INCREASE:g})")
    parser.add_argument("--half-min-net", type=float, default=HALF_MIN_NET,
                        help="min deficit growth in each half of the "
                             f"window, frags (default {HALF_MIN_NET:g})")
    parser.add_argument("--grid-s", type=float, default=GRID_S,
                        help=f"resampling step seconds (default {GRID_S:g})")
    parser.add_argument("--break-search-s", type=float,
                        default=BREAK_SEARCH_S,
                        help="seconds past a trend to search for its break "
                             f"(default {BREAK_SEARCH_S:g})")
    parser.add_argument("--min-recovery", type=float, default=MIN_RECOVERY,
                        help="deficit drop that makes a reversal material, "
                             f"frags (default {MIN_RECOVERY:g})")
    parser.add_argument("--break-join-s", type=float, default=BREAK_JOIN_S,
                        help="carrier-kill join slack around the break, "
                             f"seconds (default {BREAK_JOIN_S:g})")
    parser.add_argument("--min-carrier-kills", type=int,
                        default=MIN_CARRIER_KILLS,
                        help="carrier kills required of the breaker "
                             f"(default {MIN_CARRIER_KILLS})")
    args = parser.parse_args(argv)
    if args.grid_s <= 0:
        parser.error(f"--grid-s must be > 0 (got {args.grid_s:g})")
    if args.window_s <= 0:
        parser.error(f"--window-s must be > 0 (got {args.window_s:g})")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = common.MvdApiClient(args.api_url or common.DEFAULT_API_URL)
    result = analyze(client, args.sha,
                     window_s=args.window_s,
                     min_slope_fpm=args.min_slope,
                     min_net_increase=args.min_net_increase,
                     half_min_net=args.half_min_net,
                     grid_s=args.grid_s,
                     break_search_s=args.break_search_s,
                     min_recovery=args.min_recovery,
                     break_join_s=args.break_join_s,
                     min_carrier_kills=args.min_carrier_kills)

    if args.explain:
        print(f"trends found: {result['trends_found']}, candidates: "
              f"{len(result['candidates'])}, rejections: "
              f"{len(result['rejections'])}", file=sys.stderr)
        for rej in result["rejections"]:
            print(json.dumps(rej, indent=2), file=sys.stderr)

    candidates = result["candidates"]
    if not candidates:
        print("no qualifying comebacks", file=sys.stderr)
        return

    for cand in candidates[: args.limit]:
        cand_id = common.emit_candidate(cand, db_path=args.db,
                                        dry_run=args.dry_run)
        if cand_id is not None:
            print(cand_id)


if __name__ == "__main__":
    main()
