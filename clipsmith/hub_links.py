"""clipsmith hub deep-link resolver — demo_sha -> hub.quakeworld.nu URL.

Resolves the ONLINE hub gameId for a candidate's demo_sha and builds the
review deep link from docs/pipeline-spec.md "Candidate contract":

    https://hub.quakeworld.nu/games/?gameId=<id>&from=<sec>&track=<userid>

(format proven in local-hub frontend/src/pages/games/player/Share.tsx and
verified against a real hub Share dialog 2026-06-10).

Resolution path (documented choice)
    Direct read-only query against the hub Supabase PostgREST surface
    (`v1_games`), filtering `demo_sha256=eq.<sha>` — the table exposes the
    demo sha directly, so one GET gives an exact match. This is the pattern
    from mvd_analyzer mvd-mcp/supabase.go. The alternative — the local
    mvd-api on 127.0.0.1:8080 — was investigated and rejected: its HTTP
    router (mvd-api/router.go) has no search route at all; `searchGames`
    exists only in the MCP shim, which itself queries this same Supabase
    endpoint (and filters by player/team/map, not sha). Querying Supabase
    directly is therefore both the only sha-capable path and the one with
    no local-service dependency. Allowed per docs/decision-log.md
    "Hub Supabase allowed for deep-link resolution only".

Credentials
    The anon key is PUBLIC — it ships in the hub web bundle (see
    mvd-mcp/supabase.go) — so it is embedded here like in mvd-mcp.
    Override endpoint/key with HUB_SUPABASE_URL / HUB_SUPABASE_ANON_KEY
    env vars if the hub rotates them. Values are never printed.

URL parameters
    from  = DEMO-relative seconds: floor(window.start_s + countdown offset),
            clamped to >= 0. VERIFIED 2026-06-10 against the hub's own Share
            dialog: `from=665` corresponded to match clock 10:55 (655 s), so
            from = match-relative + countdown. The offset is read from the
            mvd-api overview `matchStart` (ms) for the demo; if mvd-api can't
            answer, DEFAULT_COUNTDOWN_S (10 s, the KTX standard) is used.
    track = pov_player.userid.

Graceful degradation
    sha not on the hub (yet) -> hub_url stays "" and the CLI exits with
    EXIT_NOT_FOUND (3) and a distinct message, so the digest builder (P1.9)
    can retry later and must render a "no hub link available" note for such
    candidates. Network/HTTP failures exit 1 (genuine error, not a miss).

Usage:
    python -m clipsmith.hub_links --sha <sha> --from <sec> --track <userid>
    python -m clipsmith.hub_links --candidate <id> [--db PATH]
        # loads the candidate from the store, resolves, fills its hub_url
"""
import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HUB_BASE_URL = "https://hub.quakeworld.nu/games/"

DEFAULT_MVD_API_URL = "http://127.0.0.1:8080"
# Standard KTX countdown — fallback when mvd-api can't supply matchStart.
DEFAULT_COUNTDOWN_S = 10.0

# hub.quakeworld.nu Supabase (PostgREST). The anon key is public — same
# one shipped in the hub web bundle (see mvd_analyzer mvd-mcp/supabase.go).
DEFAULT_SUPABASE_URL = "https://ncsphkjfominimxztjip.supabase.co/rest/v1/v1_games"
DEFAULT_SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5jc3Boa2pmb21pbmlteHp0amlwIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE2OTY5Mzg1NjMsImV4cCI6MjAxMjUxNDU2M30."
    "NN6hjlEW-qB4Og9hWAVlgvUdwrbBO13s8OkAJuBGVbo"
)

EXIT_NOT_FOUND = 3  # sha not on the hub yet — retry later (1 = real error, 2 = usage)
TIMEOUT_S = 30


class HubLookupError(Exception):
    """Network/HTTP/decode failure talking to the hub (NOT a miss)."""


def _default_http_get(url, headers):
    """GET url with headers, return response body bytes. Raises HubLookupError."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise HubLookupError(f"hub lookup: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise HubLookupError(f"hub lookup: {e}") from e


def resolve_game_id(demo_sha, http_get=None):
    """Resolve a demo sha256 to the hub gameId.

    Returns the integer gameId, or None when the game is not on the hub
    (graceful miss). Raises HubLookupError on network/HTTP/decode failure.
    """
    demo_sha = (demo_sha or "").strip().lower()
    if not demo_sha:
        raise ValueError("empty demo_sha")
    if http_get is None:
        http_get = _default_http_get

    base = os.environ.get("HUB_SUPABASE_URL", DEFAULT_SUPABASE_URL)
    key = os.environ.get("HUB_SUPABASE_ANON_KEY", DEFAULT_SUPABASE_ANON_KEY)
    query = urllib.parse.urlencode({
        "select": "id",
        "demo_sha256": f"eq.{demo_sha}",
        "limit": "1",
    })
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "accept-profile": "public",
    }
    body = http_get(f"{base}?{query}", headers)
    try:
        rows = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        raise HubLookupError(f"hub lookup: bad JSON: {e}") from e
    if not isinstance(rows, list):
        raise HubLookupError(f"hub lookup: unexpected response shape: {type(rows).__name__}")
    if not rows:
        return None
    game_id = rows[0].get("id")
    if game_id is None:
        raise HubLookupError("hub lookup: row missing id")
    return int(game_id)


def demo_time_offset(demo_sha, api_url=None, http_get=None):
    """Demo-relative offset in seconds for a demo (the countdown length).

    The hub player's `from` is demo-relative while candidate windows are
    match-relative; the difference is the countdown before matchStart.
    Tries the mvd-api overview `matchStart` (ms) first, but note the current
    mvd-api (schemaVersion 12) reports matchStart=0 — its whole timeline is
    already match-relative — so in practice DEFAULT_COUNTDOWN_S (10 s, the
    KTX standard) applies; verified against a real hub Share dialog
    2026-06-10 (from=665 at match clock 10:55 on gameId 220959).
    """
    base = (api_url or os.environ.get("CLIPSMITH_MVD_API",
                                      DEFAULT_MVD_API_URL)).rstrip("/")
    url = f"{base}/v1/demos/sha:{demo_sha}/overview"
    try:
        body = (http_get or _default_http_get)(url, {})
        overview = json.loads(body)
        if isinstance(overview, dict):
            match_start_ms = overview.get("matchStart") or 0
            if match_start_ms > 0:
                return match_start_ms / 1000.0
    except (HubLookupError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return DEFAULT_COUNTDOWN_S


def derive_from(start_s, demo_offset_s=0.0):
    """`from` query param: floor(match-relative start + demo offset), >= 0."""
    return max(0, math.floor(float(start_s) + float(demo_offset_s)))


def build_hub_url(game_id, start_s, userid, demo_offset_s=0.0):
    """Build the deep link for a resolved gameId."""
    return (
        f"{HUB_BASE_URL}?gameId={int(game_id)}"
        f"&from={derive_from(start_s, demo_offset_s)}&track={int(userid)}"
    )


def resolve_hub_url(demo_sha, start_s, userid, http_get=None,
                    demo_offset_s=None):
    """sha + window start + userid -> deep link, or "" when not on hub.

    ``demo_offset_s=None`` resolves the countdown offset via mvd-api
    (see demo_time_offset); pass an explicit value to skip that lookup.
    """
    game_id = resolve_game_id(demo_sha, http_get=http_get)
    if game_id is None:
        return ""
    if demo_offset_s is None:
        demo_offset_s = demo_time_offset(demo_sha)
    return build_hub_url(game_id, start_s, userid, demo_offset_s)


def fill_candidate(cand_id, db_path=None, http_get=None, demo_offset_s=None):
    """Resolve and persist hub_url for a stored candidate.

    Returns the hub_url ("" on miss — candidate keeps an empty hub_url so
    the digest builder renders a "no hub link available" note and a later
    run can retry).
    """
    from clipsmith import store  # local import: keep module importable standalone

    cand = store.get_candidate(cand_id, db_path=db_path)
    hub_url = resolve_hub_url(
        cand["demo_sha"],
        cand["window"]["start_s"],
        cand["pov_player"]["userid"],
        http_get=http_get,
        demo_offset_s=demo_offset_s,
    )
    store.set_hub_url(cand_id, hub_url, db_path=db_path)
    return hub_url


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m clipsmith.hub_links",
        description="Resolve hub.quakeworld.nu deep links for candidates",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sha", help="demo sha256 to resolve")
    mode.add_argument("--candidate", metavar="ID",
                      help="candidate id from the store: resolve and fill its hub_url")
    parser.add_argument("--from", dest="from_s", type=float,
                        help="window start in match seconds (with --sha)")
    parser.add_argument("--track", type=int,
                        help="POV player userid (with --sha)")
    parser.add_argument("--offset", type=float, default=None,
                        help="demo countdown offset in seconds (default: ask "
                             "mvd-api for matchStart, fallback 10)")
    parser.add_argument("--db", help="store DB path (with --candidate)")
    args = parser.parse_args(argv)

    try:
        if args.sha:
            if args.from_s is None or args.track is None:
                parser.error("--sha requires --from and --track")
            url = resolve_hub_url(args.sha, args.from_s, args.track,
                                  demo_offset_s=args.offset)
        else:
            url = fill_candidate(args.candidate, db_path=args.db,
                                 demo_offset_s=args.offset)
    except HubLookupError as e:
        sys.exit(f"error: {e}")
    except ValueError as e:
        sys.exit(f"error: {e}")

    if not url:
        print("not on hub yet: game for this sha not found (retry later)",
              file=sys.stderr)
        sys.exit(EXIT_NOT_FOUND)
    print(url)


if __name__ == "__main__":
    main()
