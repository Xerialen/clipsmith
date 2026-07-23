"""clipsmith candidate describer (pipeline-spec.md "Candidate contract",
issue #10).

Turns a stored candidate's ``metrics`` JSON into the digest-quality
``description`` the Discord review loop embeds verbatim. Every description
covers, in order:

1. **What happens** — kill counts, victims (named when few, summarized when
   many), weapon highlights, mm:ss match timing.
2. **Whose POV to watch** — a ``POV:`` sentence with the player and the
   match context (teams + scores + matchdate).
3. **Why it was recommended** — a ``Why:`` sentence with the metrics that
   ranked it (rank within the demo, the category's scoring inputs, score).

Deterministic and template-based: one renderer per category (QUAD RUNNER,
TEAM KILLER, PENT GRIND, PENT RUNNER; COME-BACK deferred with Phase 1),
fed purely from the candidate's ``metrics`` + contract fields — no mvd-api
calls, no LLM. Missing/partial metrics degrade to whatever is known; the
renderer never raises on absent fields.

LLM enrichment seam (Phase 2, OFF by default)
    ``describe(candidate, polish=None)`` — pass a callable
    ``polish(template_text, candidate) -> str`` to post-process the
    deterministic text (the template output becomes the LLM's grounding
    input). Nothing in Phase 1 supplies one.

CLI
    python -m clipsmith.describer --candidate <id> [--db PATH] [--dry-run]
        rewrite one candidate's description in the store
    python -m clipsmith.describer --all-stubs [--status S] [--category C]
        [--db PATH] [--dry-run]
        rewrite every candidate whose description still looks like a
        detector stub (optionally narrowed by --status/--category)

    --dry-run prints the new descriptions without writing to the store.
"""
import argparse
import re
import sys

from clipsmith import store

# Map weapon codes (getFrags ``weapon``) to obituary-style nouns.
WEAPON_NOUNS = {
    "rl": "a rocket",
    "gl": "a grenade",
    "lg": "the shaft",
    "sg": "a shotgun blast",
    "ssg": "a super-shotgun blast",
    "ng": "nails",
    "sng": "super nails",
    "axe": "the axe",
}

# Name victims individually up to this many kills; summarize beyond it.
FEW_VICTIMS = 3

# The one-line stub formats the Phase-1 detectors write at emit time
# (quad_runner / team_killer / pent_grind / pent_runner — keep in sync).
_STUB_PATTERNS = [re.compile(p) for p in (
    r".+'s quad run on .+ at \d+:\d{2}: \d+ frags \(\d+ RL\)\.",
    r".+ killed \d+ teammates? on .+ at \d+:\d{2}(?: within \d+ s)?"
    r" \(\d+ with RL/LG\)\.",
    r"The pent grind on .+ at \d+:\d{2}: \d+ death\(s\) in the"
    r" \d+(?:\.\d+)? s fight before .+ took it\.",
    r".+'s pent run on .+ at \d+:\d{2}: \d+ frags"
    r" \(\d+ on RL/LG carriers\)\.",
)]


# ------------------------------------------------------------ small helpers

def _mmss(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _fmt_score(score):
    """7.0 -> '7', 2.2207750000000006 -> '2.22'."""
    try:
        f = float(score)
    except (TypeError, ValueError):
        return str(score)
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}".rstrip("0").rstrip(".")


def _plural(n, noun, plural=None):
    return f"{n} {noun if n == 1 else (plural or noun + 's')}"


def _join_names(names):
    names = list(names)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _counted_names(names):
    """['a', 'b', 'a'] -> 'a x2 and b' (order of first appearance)."""
    counts = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    return _join_names(n if c == 1 else f"{n} x{c}"
                       for n, c in counts.items())


def _pov_sentence(cand, team=None):
    """The shared 'whose POV to watch' sentence."""
    name = (cand.get("pov_player") or {}).get("name") or "unknown player"
    pov = f"{name} ({team})" if team else name
    teams = cand.get("teams") or []
    bits = []
    if len(teams) >= 2:
        t0, t1 = teams[0], teams[1]
        bits.append(f"{t0.get('name', '?')} vs {t1.get('name', '?')}"
                    f" ({t0.get('score', '?')}-{t1.get('score', '?')})")
    if cand.get("matchdate"):
        bits.append(cand["matchdate"])
    return f"POV: {pov}" + (f" — {', '.join(bits)}" if bits else "") + "."


def _rank_clause(rank, total, what_singular, superlative):
    """'highest-scoring quad run in this demo (20 runs)' or
    'ranked #2 of 20 quad runs in this demo'; degrades when rank/total
    are missing."""
    if rank == 1:
        clause = f"the {superlative} {what_singular} in this demo"
        if total:
            clause += f" ({_plural(total, what_singular.split()[-1])})"
        return clause
    if rank and total:
        return (f"ranked #{rank} of {total} {what_singular.split()[-1]}s"
                " in this demo")
    return f"a top {what_singular} in this demo"


def _finishing_clause(kills):
    """', finishing with a rocket on Hto' from the last per-kill event."""
    if not kills:
        return ""
    last = kills[-1]
    victim = last.get("victim")
    if not victim:
        return ""
    noun = WEAPON_NOUNS.get(last.get("weapon"))
    if noun:
        return f", finishing with {noun} on {victim}"
    return f", finishing on {victim}"


def _kills_span_clause(kills, start_s):
    """' in 27s' — pickup/first-event to last kill."""
    if not kills or start_s is None:
        return ""
    span = kills[-1].get("t_s", start_s) - start_s
    if span <= 0:
        return ""
    return f" in {span:.0f}s"


# ------------------------------------------------------ powerup-run template

def _powerup_run(cand, m, powerup, pickup_key):
    """Shared what-sentence for QUAD RUNNER / PENT RUNNER (categories 1, 4):
    grab -> rampage -> finishing kill."""
    pov = (cand.get("pov_player") or {}).get("name") or "unknown player"
    map_name = cand.get("map") or "?"
    pickup_s = m.get(pickup_key)
    kills = m.get("kills") or []
    n = m.get("frags_in_run")
    if n is None and kills:
        n = len(kills)

    at = f" at {_mmss(pickup_s)}" if pickup_s is not None else ""
    grab = f"{pov} grabs {powerup}{at} on {map_name}"
    if not n:
        return grab + (" but the run goes dry — no frags." if n == 0
                       else ".")
    span = _kills_span_clause(kills, pickup_s)
    victims = [k.get("victim") for k in kills]
    if len(kills) <= FEW_VICTIMS and all(victims):
        return (f"{grab} and frags {_counted_names(victims)}{span}"
                f"{_finishing_clause(kills) if len(kills) > 1 else ''}.")
    return (f"{grab} and tears through {_plural(n, 'frag')}{span}"
            f"{_finishing_clause(kills)}.")


# --------------------------------------------------------- category renderers

def _quad_runner(cand, m):
    what = _powerup_run(cand, m, "quad", "quad_pickup_s")

    bits = []
    n = m.get("frags_in_run")
    if n is not None:
        frag_bit = _plural(n, "frag")
        rl = m.get("rl_kills")
        if rl:
            frag_bit += f" incl. {_plural(rl, 'RL kill')}"
        bits.append(frag_bit)
    if cand.get("score") is not None:
        bits.append(f"score {_fmt_score(cand['score'])}")
    why = ("Why: "
           + _rank_clause(m.get("rank"), m.get("total_quad_runs"),
                          "quad run", "highest-scoring")
           + (f" — {', '.join(bits)}" if bits else "") + ".")
    return f"{what} {_pov_sentence(cand)} {why}"


def _team_killer(cand, m):
    pov = (cand.get("pov_player") or {}).get("name") or "unknown player"
    map_name = cand.get("map") or "?"
    kills = m.get("kills") or []
    n = m.get("tk_count")
    if n is None and kills:
        n = len(kills)
    first_s = m.get("first_tk_s")
    span_s = m.get("span_s")
    victims = [k.get("victim") for k in kills if k.get("victim")]
    rl_lg = m.get("rl_lg_tks") or 0

    if not n:
        what = f"{pov} turns on the team on {map_name}."
    elif n == 1:
        victim = f" {victims[0]}" if victims else ""
        at = f" at {_mmss(first_s)}" if first_s is not None else ""
        weapon = (f" with {WEAPON_NOUNS[kills[0]['weapon']]}"
                  if kills and kills[0].get("weapon") in WEAPON_NOUNS else "")
        what = f"{pov} takes out teammate{victim} on {map_name}{at}{weapon}."
    else:
        span = (f" in {span_s:.0f}s" if span_s is not None else "")
        at = f" starting at {_mmss(first_s)}" if first_s is not None else ""
        named = (f" — {_counted_names(victims)} fall to friendly fire"
                 if victims and n <= FEW_VICTIMS else "")
        weapon = f", {rl_lg} with RL/LG" if rl_lg else ""
        what = (f"{pov} takes out {_plural(n, 'teammate')} on {map_name}"
                f"{span}{at}{named}{weapon}.")

    criteria = m.get("criteria") or {}
    reasons = []
    if criteria.get("burst_20s"):
        reasons.append("multiple teamkills inside 20s")
    if criteria.get("rl_lg_30s"):
        reasons.append("an RL/LG teamkill")
    bits = []
    if n is not None:
        tk_bit = _plural(n, "teamkill")
        if span_s is not None and n > 1:
            tk_bit += f" in {span_s:.1f}s"
        bits.append(tk_bit)
    if reasons:
        bits.append(f"qualifies via {' + '.join(reasons)}")
    if cand.get("score") is not None:
        bits.append(f"score {_fmt_score(cand['score'])}")
    why = ("Why: "
           + _rank_clause(m.get("rank"), m.get("total_incidents"),
                          "teamkill incident", "worst")
           + (f" — {', '.join(bits)}" if bits else "") + ".")
    return f"{what} {_pov_sentence(cand)} {why}"


def _pent_grind(cand, m):
    map_name = cand.get("map") or "?"
    taker = (m.get("pent_taker")
             or (cand.get("pov_player") or {}).get("name")
             or "unknown player")
    avail_s = m.get("pent_available_s")
    taken_s = m.get("pent_taken_s")
    dur_s = m.get("contest_duration_s")
    deaths_total = m.get("deaths_total")
    deaths = m.get("deaths") or []
    # The detector counts deaths whose near_pent is not False (True or
    # unresolved); mirror that when naming victims.
    counted = [d for d in deaths if d.get("near_pent") is not False]
    by_team = m.get("deaths_by_team") or {}
    if deaths_total is None and counted:
        deaths_total = len(counted)

    spawn = (f"The pent on {map_name} spawns at {_mmss(avail_s)}"
             if avail_s is not None else f"The pent on {map_name} spawns")
    grab = f"{taker} grabs it" + (f" at {_mmss(taken_s)}"
                                  if taken_s is not None else "")
    if deaths_total:
        victims = [d.get("victim") for d in counted]
        if deaths_total <= FEW_VICTIMS and victims and all(victims):
            toll = (f"the fight for it kills {_counted_names(victims)}"
                    f" before {grab}")
        else:
            team_bits = ", ".join(f"{c} {t}" for t, c in by_team.items())
            toll = (f"the fight for it claims {_plural(deaths_total, 'death')}"
                    + (f" ({team_bits})" if team_bits else "")
                    + f" before {grab}")
        if dur_s is not None:
            what = f"{spawn} and, over the next {dur_s:.1f}s, {toll}."
        else:
            what = f"{spawn} and {toll}."
    else:
        what = f"{spawn} and {grab} uncontested."

    pov = _pov_sentence(cand, team=m.get("pent_taker_team"))

    bits = []
    if deaths_total is not None:
        bit = _plural(deaths_total, "death") + " near the pent"
        if dur_s is not None:
            bit += f" in the {dur_s:.1f}s contest"
        bits.append(bit)
    if cand.get("score") is not None:
        bits.append(f"score {_fmt_score(cand['score'])}")
    why = ("Why: "
           + _rank_clause(m.get("rank"), m.get("total_pent_phases"),
                          "pent contest", "bloodiest")
           + (f" — {', '.join(bits)}" if bits else "") + ".")
    return f"{what} {pov} {why}"


def _pent_runner(cand, m):
    what = _powerup_run(cand, m, "pent", "pent_pickup_s")

    bits = []
    n = m.get("frags_in_run")
    if n is not None:
        frag_bit = _plural(n, "frag")
        carriers = m.get("carrier_kills")
        if carriers:
            detail_bits = []
            if m.get("rl_carrier_kills"):
                detail_bits.append(f"{m['rl_carrier_kills']} RL")
            if m.get("lg_carrier_kills"):
                detail_bits.append(f"{m['lg_carrier_kills']} LG")
            detail = f" ({', '.join(detail_bits)})" if detail_bits else ""
            frag_bit += (f" incl. {_plural(carriers, 'kill')}"
                         f" on RL/LG carriers{detail}")
        bits.append(frag_bit)
    if cand.get("score") is not None:
        bits.append(f"score {_fmt_score(cand['score'])}")
    why = ("Why: "
           + _rank_clause(m.get("rank"), m.get("total_pent_runs"),
                          "pent run", "highest-scoring")
           + (f" — {', '.join(bits)}" if bits else "") + ".")
    return f"{what} {_pov_sentence(cand)} {why}"


def _generic(cand, m):
    """Fallback for unknown categories / absent metrics: describe what the
    contract fields alone support — never crash."""
    pov = (cand.get("pov_player") or {}).get("name") or "unknown player"
    category = (cand.get("category") or "highlight").lower()
    start_s = (cand.get("window") or {}).get("start_s")
    at = f" at {_mmss(start_s)}" if start_s is not None else ""
    what = f"{pov}'s {category} moment on {cand.get('map') or '?'}{at}."
    why = ("Why: " + (f"score {_fmt_score(cand['score'])}, "
                      if cand.get("score") is not None else "")
           + f"picked by the {category} detector.")
    return f"{what} {_pov_sentence(cand)} {why}"


RENDERERS = {
    "QUAD RUNNER": _quad_runner,
    "TEAM KILLER": _team_killer,
    "PENT GRIND": _pent_grind,
    "PENT RUNNER": _pent_runner,
    # COME-BACK: deferred (pipeline-spec category 5 is post-Phase-1)
}


# ----------------------------------------------------------------- public API

def render_template(cand):
    """Deterministic template text for one candidate dict (store schema)."""
    renderer = RENDERERS.get(cand.get("category"), _generic)
    return renderer(cand, cand.get("metrics") or {})


def describe(cand, polish=None):
    """Candidate dict -> description string (what / POV / why).

    ``polish``: optional LLM-enrichment hook ``(text, candidate) -> str``
    applied to the template output (Phase-2 seam). Default None = pure
    deterministic template.
    """
    text = render_template(cand)
    if polish is not None:
        text = polish(text, cand)
    return text


def looks_like_stub(description):
    """True when a description is empty or matches one of the one-line
    stub formats the Phase-1 detectors emit (see _STUB_PATTERNS)."""
    d = (description or "").strip()
    if not d:
        return True
    return any(p.fullmatch(d) for p in _STUB_PATTERNS)


def describe_candidate(cand_id, db_path=None, dry_run=False, polish=None):
    """Load one candidate, regenerate its description, write it back.

    Returns the new description. ``dry_run=True`` skips the store write.
    """
    cand = store.get_candidate(cand_id, db_path=db_path)
    text = describe(cand, polish=polish)
    if not dry_run:
        store.set_description(cand_id, text, db_path=db_path)
    return text


def describe_stubs(status=None, category=None, db_path=None, dry_run=False,
                   polish=None):
    """Regenerate every stub-looking description (post-pass over the store).

    Returns ``[(id, new_description), ...]`` for the candidates rewritten.
    """
    out = []
    for cand in store.list_candidates(category=category, status=status,
                                      db_path=db_path):
        if not looks_like_stub(cand.get("description")):
            continue
        text = describe(cand, polish=polish)
        if not dry_run:
            store.set_description(cand["id"], text, db_path=db_path)
        out.append((cand["id"], text))
    return out


# ------------------------------------------------------------------------ CLI

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipsmith.describer",
        description="rewrite candidate descriptions from their metrics",
    )
    parser.add_argument("--db", help="store DB path (default: "
                        "state/candidates.db, or $CLIPSMITH_DB)")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--candidate", metavar="ID",
                        help="rewrite this candidate's description")
    target.add_argument("--all-stubs", action="store_true",
                        help="rewrite every candidate whose description "
                             "still looks like a detector stub")
    parser.add_argument("--status",
                        help="with --all-stubs: only candidates in this "
                             "status (e.g. candidate)")
    parser.add_argument("--category",
                        help="with --all-stubs: only this category")
    parser.add_argument("--dry-run", action="store_true",
                        help="print new descriptions without writing "
                             "to the store")
    args = parser.parse_args(argv)
    if args.candidate and (args.status or args.category):
        parser.error("--status/--category only apply to --all-stubs")

    try:
        if args.candidate:
            text = describe_candidate(args.candidate, db_path=args.db,
                                      dry_run=args.dry_run)
            print(text)
        else:
            rewritten = describe_stubs(status=args.status,
                                       category=args.category,
                                       db_path=args.db,
                                       dry_run=args.dry_run)
            for cand_id, text in rewritten:
                print(f"{cand_id}\t{text}")
            if not rewritten:
                print("no stub descriptions found", file=sys.stderr)
    except ValueError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
