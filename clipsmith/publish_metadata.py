"""clipsmith publish-metadata generator (pipeline-spec.md "Publish stage",
issue #27).

Turns a rendered candidate (store record, normally status ``rendered``) into
the YouTube metadata the upload automation needs: title, hashtags, and a full
description, emitted as ``<mp4 basename>.metadata.json`` next to the rendered
MP4.

Content contract (pipeline-spec "Publish stage" + issue #27):

Title
    ``<CATEGORY> OF THE WEEK — Week NN — <player> (<map>)``, hard-asserted
    <= 100 chars (YouTube limit per vault ``podcasting/channel-art-specs.md``).
    If a long player/map name pushes it over, the names are ellipsized —
    the map keeps at least a useful stub, the player gets the rest of the
    budget — and the fixed pattern parts are never cut.

Description (in order, per the spec)
    1. **Hook** — what the event is + the player, front-loaded into the first
       ~157 chars (YouTube shows ~157 description chars before the
       "Show more" fold). The hook is a self-contained sentence ending in
       the category + week, hard-asserted <= 157 chars, and is always the
       first paragraph, so the pre-fold text never cuts mid-thought.
    2. The candidate's human-readable ``description`` from the store
       (the issue-#10 describer text), plus the hub deep link when present.
    3. Community links: the quake.world Discord, the QuakeWorld wiki,
       how to start playing again (nQuake download).
    4. OneMoreQUAD channel cross-links: the PLAY OF THE WEEK playlist
       (placeholder id until the owner creates it — placeholders never block
       the build, pipeline-spec "Placeholders"), the @OneMoreQUAD handle and
       the channel-id URL.
    5. Hashtags (YouTube renders the first 3 above the title; > 15 in a
       description means ALL are ignored, so the list stays small).
    Total <= 5000 chars (asserted); if an oversized store description would
    push past the limit, that paragraph alone is ellipsized to fit — the
    links and hashtags are never dropped.

Tags vs hashtags (decision)
    The JSON carries BOTH ``hashtags`` (with ``#``, embedded verbatim at the
    end of the description) and ``tags`` (plain keywords for the YouTube
    tags box, which takes no ``#``; <= 500 chars total per the vault doc).
    The issue says "tags/hashtags" — emitting both representations lets the
    Phase-4 upload automation use either without re-deriving.

Week number
    Derived from the candidate the same way the weekly digest does
    (``digest.candidate_week_date``: ``matchdate`` governs, ``detected_at``
    UTC date is the fallback), formatted as an ISO week id (``2026-W24``).
    Overridable with ``--week`` for re-publishes.

Category extensibility (issue #27 task 3)
    One ``CATEGORY_TEMPLATES`` entry per category (hook builder + category
    hashtag). COME-BACK (Phase 5) slots in by adding ONE entry — no other
    code changes. Unknown categories degrade to a generic hook (never
    crash), mirroring describer._generic.

Determinism
    The metadata is a pure function of the candidate dict + week id — no
    timestamps — so golden-file tests can assert byte-identical output.
    Files are written with LF newlines explicitly (this repo is developed
    on Windows with autocrlf; golden bytes must not depend on the host).

Status note
    The publish stage operates on status ``rendered``; the CLI warns on
    stderr when the candidate is in any other status but still generates
    (preview-friendly — the render orchestrator owns lifecycle enforcement,
    this module never writes to the store).

Usage:
    python -m clipsmith.publish_metadata --candidate <id> [--db PATH]
        [--mp4 PATH] [--out PATH] [--week 2026-W24]

    Three output modes:
    - --mp4 PATH: writes <basename>.metadata.json next to the (future) MP4;
    - --out PATH: writes to an explicit path (overrides --mp4 placement);
    - neither flag: prints the metadata JSON to stdout (preview mode —
      nothing is written; diagnostics go to stderr so stdout stays pipeable).
"""
import argparse
import json
import re
import sys
from pathlib import Path

from clipsmith import digest, store

# --------------------------------------------------------------- constants

# YouTube limits — vault podcasting/channel-art-specs.md ("Text / metadata").
TITLE_MAX = 100          # video title <= 100 chars
DESCRIPTION_MAX = 5000   # description <= 5000 chars
HOOK_MAX = 157           # ~157 chars shown before the "Show more" fold
TAGS_MAX_CHARS = 500     # tags <= 500 chars total
HASHTAGS_MAX = 15        # >15 hashtags in a description = ALL ignored

# Community links (pipeline-spec "Publish stage" content contract).
DISCORD_URL = "https://discord.quake.world/"
WIKI_URL = "https://www.quakeworld.nu/wiki/"
NQUAKE_URL = "https://nquake.com/"

# OneMoreQUAD channel cross-links (issue #27).
CHANNEL_HANDLE_URL = "https://www.youtube.com/@OneMoreQUAD"
CHANNEL_ID = "UCpUbPTu3tGkZXgXhuXILzYw"
CHANNEL_ID_URL = f"https://www.youtube.com/channel/{CHANNEL_ID}"
# Placeholder until the owner creates the playlist (pipeline-spec
# "Placeholders": never blocks the build; HITL issue swaps the real id in).
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLACEHOLDER_PLAY_OF_THE_WEEK"

_WEEK_NO_RE = re.compile(r"^\d{4}-W(\d{2})$")


# ----------------------------------------------------------- small helpers

def _ellipsize(text, limit):
    """Trim text to <= limit chars, ending in an ellipsis when cut."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    return text[: limit - 1] + "…"


def _plural(n, noun, plural=None):
    return f"{n} {noun if n == 1 else (plural or noun + 's')}"


def _pov_name(cand):
    return (cand.get("pov_player") or {}).get("name") or "unknown player"


def _week_no(week_id):
    """'2026-W24' -> 24 (digest.parse_week has already validated it)."""
    return int(_WEEK_NO_RE.match(week_id).group(1))


def week_for_candidate(cand):
    """The ISO week id ('2026-W24') a candidate belongs to.

    Same governance as the weekly digest: matchdate, falling back to the
    UTC date of detected_at. Raises ValueError when neither parses (the
    store always fills detected_at, so this is a data-corruption signal).
    """
    date = digest.candidate_week_date(cand)
    if date is None:
        raise ValueError(
            f"candidate {cand.get('id')!r} has no parseable matchdate or "
            "detected_at — pass --week explicitly")
    return digest.format_week(date)


# ------------------------------------------------------- category templates
# One entry per category: the pre-fold hook builder + the category hashtag.
# COME-BACK (Phase 5) slots in by adding ONE entry here — nothing else.
#
# Hook-builder contract: (cand, metrics) -> the VARIABLE event clause only,
# no punctuation, no category/week tail. build_hook() appends the fixed
# "— <CATEGORY> OF THE WEEK, Week NN." suffix and ellipsizes only the event
# clause when over the pre-fold budget — the suffix is never cut.

def _hook_quad_runner(cand, m):
    player, map_name = _pov_name(cand), cand.get("map") or "?"
    n = m.get("frags_in_run")
    if n is None and m.get("kills"):
        n = len(m["kills"])
    return (f"{player} grabs quad on {map_name} and tears through "
            f"{_plural(n, 'frag')}" if n
            else f"{player}'s quad run on {map_name}")


def _hook_team_killer(cand, m):
    player, map_name = _pov_name(cand), cand.get("map") or "?"
    n = m.get("tk_count")
    if n is None and m.get("kills"):
        n = len(m["kills"])
    return (f"{player} takes out {_plural(n, 'teammate')} on {map_name}"
            if n else f"{player} turns on the team on {map_name}")


def _hook_pent_grind(cand, m):
    map_name = cand.get("map") or "?"
    taker = m.get("pent_taker") or _pov_name(cand)
    n = m.get("deaths_total")
    if n is None and m.get("deaths"):
        n = len(m["deaths"])
    return (f"The fight for pent on {map_name} claims "
            f"{_plural(n, 'life', 'lives')} before {taker} grabs it"
            if n else f"The scrap for pent on {map_name}")


def _hook_pent_runner(cand, m):
    player, map_name = _pov_name(cand), cand.get("map") or "?"
    n = m.get("frags_in_run")
    if n is None and m.get("kills"):
        n = len(m["kills"])
    return (f"{player} grabs pent on {map_name} and racks up "
            f"{_plural(n, 'frag')}" if n
            else f"{player}'s pent run on {map_name}")


def _hook_generic(cand, m):
    """Unknown-category fallback — describe what the contract fields alone
    support; never crash (mirrors describer._generic)."""
    player, map_name = _pov_name(cand), cand.get("map") or "?"
    category = (cand.get("category") or "HIGHLIGHT").upper()
    return f"{player}'s {category.lower()} moment on {map_name}"


CATEGORY_TEMPLATES = {
    "QUAD RUNNER": {"hook": _hook_quad_runner, "hashtag": "#QuadRunner"},
    "TEAM KILLER": {"hook": _hook_team_killer, "hashtag": "#TeamKiller"},
    "PENT GRIND": {"hook": _hook_pent_grind, "hashtag": "#PentGrind"},
    "PENT RUNNER": {"hook": _hook_pent_runner, "hashtag": "#PentRunner"},
    # "COME-BACK": add {"hook": ..., "hashtag": "#ComeBack"} in Phase 5 —
    # the only change needed (issue #27 task 3).
}


def _category_hashtag(category):
    """Registered hashtag, or one derived from the category words
    ('COME-BACK' -> '#ComeBack') so unknown categories still tag sanely."""
    entry = CATEGORY_TEMPLATES.get(category)
    if entry:
        return entry["hashtag"]
    words = re.split(r"[^A-Za-z0-9]+", category or "")
    name = "".join(w.capitalize() for w in words if w)
    return f"#{name}" if name else "#QW"


# ------------------------------------------------------------------ builders

def build_title(cand, week_id):
    """``<CATEGORY> OF THE WEEK — Week NN — <player> (<map>)``, <= 100 chars.

    Graceful truncation: only the player/map names are ellipsized (the map
    keeps at least a stub of max(8, a third of the name budget); the player
    gets the rest). The fixed pattern parts are never cut. Raises
    ValueError if the result still exceeds TITLE_MAX (cannot happen with
    the bounded category registry — a hard assert, per the issue).
    """
    category = cand.get("category") or "HIGHLIGHT"
    player = _pov_name(cand)
    map_name = cand.get("map") or "?"
    fixed = f"{category} OF THE WEEK — Week {_week_no(week_id):02d} — "
    budget = TITLE_MAX - len(fixed) - 3  # " (" + ")"
    if len(player) + len(map_name) > budget:
        map_budget = min(len(map_name), max(8, budget // 3))
        player = _ellipsize(player, budget - map_budget)
        map_name = _ellipsize(map_name, map_budget)
    title = f"{fixed}{player} ({map_name})"
    if len(title) > TITLE_MAX:
        raise ValueError(
            f"title exceeds {TITLE_MAX} chars after truncation: {title!r}")
    return title


def build_hook(cand, week_id):
    """The pre-fold hook: what the event is + the player, <= 157 chars,
    self-contained (one sentence, ends with category + week).

    The fixed "— <CATEGORY> OF THE WEEK, Week NN." suffix is composed AFTER
    truncation: only the variable event clause (player/map names) is
    ellipsized when over budget, so long names can never cut the required
    suffix. Raises ValueError if the suffix alone exceeds HOOK_MAX (cannot
    happen with the bounded category registry — a hard assert, mirroring
    build_title)."""
    template = CATEGORY_TEMPLATES.get(cand.get("category"), {})
    builder = template.get("hook", _hook_generic)
    category = (cand.get("category") or "HIGHLIGHT").upper()
    suffix = f" — {category} OF THE WEEK, Week {_week_no(week_id):02d}."
    budget = HOOK_MAX - len(suffix)
    if budget < 1:
        raise ValueError(
            f"hook suffix alone exceeds {HOOK_MAX} chars: {suffix!r}")
    event = _ellipsize(builder(cand, cand.get("metrics") or {}), budget)
    return event + suffix


def build_hashtags(cand):
    """Description hashtags: 2 evergreen + category + 2 channel/genre.

    YouTube surfaces the FIRST 3 above the title, so the most searchable
    tags lead; 5 total stays far under the 15-hashtag ignore-all cliff.
    """
    return ["#QuakeWorld", "#Quake", _category_hashtag(cand.get("category")),
            "#FPS", "#OneMoreQUAD"]


def build_tags(cand):
    """Plain keywords for the YouTube tags box (no '#'; <= 500 chars total)."""
    tags = ["QuakeWorld", "Quake", (cand.get("category") or "").lower(),
            "of the week", cand.get("map") or "", _pov_name(cand),
            "QuakeWorld highlights", "One More QUAD"]
    tags = [t for t in tags if t]
    while tags and sum(len(t) for t in tags) + 2 * (len(tags) - 1) > TAGS_MAX_CHARS:
        tags.pop()  # drop least-important tail tags; never exceed the cap
    return tags


def build_description(cand, week_id):
    """The full description per the spec order (see module docstring).

    <= 5000 chars asserted; an oversized store description is the only
    paragraph that gets ellipsized to fit.
    """
    hook = build_hook(cand, week_id)
    body = (cand.get("description") or "").strip()
    hub_url = cand.get("hub_url") or ""

    links_block = "\n".join([
        "QuakeWorld is the original arena FPS — and its community is alive"
        " and kicking. Come join us:",
        f"Join the QuakeWorld Discord: {DISCORD_URL}",
        f"The QuakeWorld wiki: {WIKI_URL}",
        f"Start playing again — download nQuake: {NQUAKE_URL}",
        "",
        "More from One More QUAD:",
        f"PLAY OF THE WEEK playlist: {PLAYLIST_URL}",
        f"Subscribe: {CHANNEL_HANDLE_URL}",
        f"All videos: {CHANNEL_ID_URL}",
        "",
        " ".join(build_hashtags(cand)),
    ])

    def assemble(body_text):
        parts = [hook]
        if body_text:
            parts.append(body_text)
        if hub_url:
            parts.append(f"Relive the moment in the QuakeWorld hub:\n{hub_url}")
        parts.append(links_block)
        return "\n\n".join(parts)

    description = assemble(body)
    if len(description) > DESCRIPTION_MAX:
        overshoot = len(description) - DESCRIPTION_MAX
        description = assemble(_ellipsize(body, max(len(body) - overshoot, 0)))
    if len(description) > DESCRIPTION_MAX:
        raise ValueError(
            f"description exceeds {DESCRIPTION_MAX} chars even after "
            "trimming the candidate text")
    return description


def generate(cand, week_id=None):
    """Candidate dict (store schema) -> metadata dict (pure, deterministic).

    Keys: candidate_id (source link-back), category, week, title, hook,
    description, hashtags, tags — everything the upload automation needs.
    """
    if week_id is None:
        week_id = week_for_candidate(cand)
    digest.parse_week(week_id)  # validate (raises ValueError on garbage)
    return {
        "candidate_id": cand["id"],
        "category": cand.get("category"),
        "week": week_id,
        "title": build_title(cand, week_id),
        "hook": build_hook(cand, week_id),
        "description": build_description(cand, week_id),
        "hashtags": build_hashtags(cand),
        "tags": build_tags(cand),
    }


# --------------------------------------------------------------- file output

def to_json(metadata):
    """Canonical serialization (stable key order, LF, trailing newline) —
    the exact bytes the golden-file tests pin."""
    return json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"


def metadata_path_for(mp4_path):
    """rendered MP4 path -> sibling metadata path (same basename):
    ``…/clip.mp4`` -> ``…/clip.metadata.json``."""
    return Path(mp4_path).with_suffix(".metadata.json")


def write_metadata(metadata, out_path):
    """Write the metadata JSON with explicit LF newlines (golden-file
    byte-stability on Windows checkouts). Returns the path written."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(to_json(metadata))
    return out_path


# ------------------------------------------------------------------------ CLI

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipsmith.publish_metadata",
        description="generate YouTube publish metadata for a rendered "
                    "candidate (issue #27)",
        epilog="with neither --mp4 nor --out, the metadata JSON is printed "
               "to stdout (preview mode; diagnostics go to stderr)",
    )
    parser.add_argument("--db", help="store DB path (default: "
                        "state/candidates.db, or $CLIPSMITH_DB)")
    parser.add_argument("--candidate", metavar="ID", required=True,
                        help="candidate id in the store")
    parser.add_argument("--week", help="ISO week id like 2026-W24 "
                        "(default: derived from the candidate's matchdate)")
    parser.add_argument("--mp4", help="rendered MP4 path — writes "
                        "<basename>.metadata.json next to it")
    parser.add_argument("--out", help="explicit output path (overrides --mp4 "
                        "placement)")
    args = parser.parse_args(argv)

    try:
        cand = store.get_candidate(args.candidate, db_path=args.db)
        if cand.get("status") != "rendered":
            print(f"note: candidate {args.candidate} has status "
                  f"{cand.get('status')!r} (publish normally runs on "
                  "'rendered'); generating anyway", file=sys.stderr)
        metadata = generate(cand, week_id=args.week)
        if args.out:
            path = write_metadata(metadata, args.out)
            print(f"metadata written: {path}", file=sys.stderr)
        elif args.mp4:
            path = write_metadata(metadata, metadata_path_for(args.mp4))
            print(f"metadata written: {path}", file=sys.stderr)
        else:
            # Documented preview mode: no --mp4/--out -> JSON on stdout
            # (stderr carries the notes, so stdout stays pipeable).
            print(to_json(metadata), end="")
    except ValueError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
