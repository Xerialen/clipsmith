"""Shared plumbing for clipsmith detectors.

Reusable across QUAD RUNNER / TEAM KILLER / PENT GRIND / PENT RUNNER:

- :class:`MvdApiClient` — thin HTTP client for mvd-api (proven access
  patterns from scripts/analysis/*; endpoint table in
  ``Xerialen/mvd_analyzer`` mvd-api/README.md).
- Window math — :func:`compute_window` (the spec's "pickup −5 s … +30 s,
  extend to end of run if frags continue" rule) as a pure function so it can
  be unit-tested offline.
- Kill filtering — :func:`filter_kills` over the getFrags kill log with
  suicide/teamkill knobs (QUAD RUNNER excludes both; TEAM KILLER *selects*
  teamkills).
- Demo metadata extraction — :func:`demo_meta` (map short name, matchdate,
  teams+scores, player→userid) from getOverview + getMetadata.
- Store emission — :func:`emit_candidate` wrapping clipsmith.store.
  Idempotent (issue #32): re-emitting a candidate whose natural key
  (:func:`candidate_key` — category + demo_sha + POV name + window) already
  exists in the store is a no-op returning the existing id, so re-running a
  detection batch never creates duplicate rows.

Unit notes (mvd-api schemaVersion 12):
- ``/frags`` kill log and ``/items`` phase fields are **milliseconds**.
- ``/events`` times (``t``, ``detail.endTime``, ``detail.duration``) are
  **seconds** (float).
- The candidate contract's ``window`` is match-relative **seconds**.
"""
import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from clipsmith import store

log = logging.getLogger("clipsmith.detectors.common")

DEFAULT_API_URL = "http://127.0.0.1:8080"


class MvdApiClient:
    """Minimal mvd-api HTTP client (no auth — local read-only API)."""

    def __init__(self, base_url=DEFAULT_API_URL, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _demo_base(self, sha):
        return f"{self.base_url}/v1/demos/sha:{sha}"

    def _request(self, url, method="GET"):
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def load_demo(self, sha):
        """POST /v1/demos/{id} — warm the cache; idempotent."""
        return self._request(self._demo_base(sha), method="POST")

    def get(self, sha, path, **params):
        """GET /v1/demos/{id}/<path>?<params> (None params dropped)."""
        url = f"{self._demo_base(sha)}/{path.lstrip('/')}"
        q = {k: v for k, v in params.items() if v is not None}
        if q:
            url += "?" + urllib.parse.urlencode(q)
        return self._request(url)

    # Named wrappers mirroring the MCP tool surface.
    def get_overview(self, sha):
        return self.get(sha, "overview")

    def get_metadata(self, sha):
        return self.get(sha, "metadata")

    def get_frags(self, sha, players=None, weapon=None):
        return self.get(sha, "frags", players=players, weapon=weapon)

    def get_items(self, sha, items=None, players=None, kinds=None):
        return self.get(sha, "items", items=items, players=players, kinds=kinds)

    def get_events(self, sha, types=None, **params):
        return self.get(sha, "events", types=types, **params)


def ms_to_s(ms):
    return ms / 1000.0


def compute_window(pickup_s, run_end_s, holder_kill_times_s,
                   pre_s=5.0, base_len_s=30.0, match_start_s=0.0,
                   match_end_s=None):
    """Pure window-math for powerup-run categories (spec categories 1 & 4).

    Window = pickup −``pre_s`` … +``base_len_s``, EXTENDED to the end of the
    run (``run_end_s``, i.e. powerup expiry) when the holder keeps fragging
    past the +``base_len_s`` mark.

    ``holder_kill_times_s`` must be the holder's scoring kill times within
    the held interval, match-relative seconds.

    Returns ``(start_s, end_s)``; start is clamped to ``match_start_s``,
    end to ``match_end_s`` when given (a powerup grabbed in the final
    seconds must not produce a render window past match end).
    """
    start_s = max(match_start_s, pickup_s - pre_s)
    base_end_s = pickup_s + base_len_s
    end_s = base_end_s
    if run_end_s > base_end_s and any(t > base_end_s for t in holder_kill_times_s):
        end_s = run_end_s
    if match_end_s is not None:
        end_s = min(end_s, match_end_s)
    return start_s, end_s


def filter_kills(frags, killer, start_ms, end_ms,
                 include_teamkills=False, include_suicides=False):
    """Kills by ``killer`` within [start_ms, end_ms] from a getFrags kill log.

    ``isTeamKill`` / ``isSuicide`` are omitempty in the API — absent means
    False. Returns the matching frag dicts in time order.
    """
    out = []
    for f in frags:
        if f.get("killer") != killer:
            continue
        if not start_ms <= f["time"] <= end_ms:
            continue
        if f.get("isSuicide", False) and not include_suicides:
            continue
        if f.get("isTeamKill", False) and not include_teamkills:
            continue
        out.append(f)
    out.sort(key=lambda f: f["time"])
    return out


_SERVERDEMO_DATE = re.compile(r"\[(?:[^\]]*)\](\d{4})(\d{2})(\d{2})-\d{4}")


def demo_meta(overview, metadata):
    """Extract the candidate-contract demo metadata.

    Returns ``{map, matchdate, teams, player_userids}``:
    - ``map``: short bsp name from serverinfo (overview carries the long
      message name, e.g. "The Abandoned Base"); falls back to overview.
    - ``matchdate``: YYYY-MM-DD from the KTX ``serverdemo`` filename
      (server-local date), falling back to the serverinfo ``epoch`` (UTC),
      else "".
    - ``teams``: ``[{name, score}]`` from overview team frag totals.
    - ``player_userids``: name → userid from overview ``playerUserIDs``.
    """
    server_info = (metadata or {}).get("serverInfo") or {}

    map_name = server_info.get("map") or overview.get("map") or ""

    matchdate = ""
    m = _SERVERDEMO_DATE.search(server_info.get("serverdemo", ""))
    if m:
        matchdate = "-".join(m.groups())
    else:
        epoch = server_info.get("epoch")
        if epoch:
            try:
                matchdate = datetime.fromtimestamp(
                    int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
            except (ValueError, OverflowError, OSError):
                pass

    teams = [{"name": t["name"], "score": t.get("frags", 0)}
             for t in overview.get("teams") or []]

    return {
        "map": map_name,
        "matchdate": matchdate,
        "teams": teams,
        "player_userids": overview.get("playerUserIDs") or {},
    }


def candidate_key(cand):
    """Natural identity key for candidate dedup (issue #32, documented choice).

    ``(category, demo_sha, pov_player.name, window.start_s, window.end_s)``
    — one candidate per detector category, per demo, per POV, per render
    window. Detector reruns recompute windows deterministically from the
    same demo data, so the same moment always maps to the same key; the
    JSON-column round-trip through the store preserves the floats exactly.
    """
    window = cand.get("window") or {}
    pov = cand.get("pov_player") or {}
    return (
        cand.get("category"),
        cand.get("demo_sha"),
        pov.get("name"),
        window.get("start_s"),
        window.get("end_s"),
    )


def find_existing_candidate(cand, db_path=None):
    """Return the stored candidate matching ``cand``'s natural key, or None."""
    key = candidate_key(cand)
    for existing in store.list_candidates(category=cand.get("category"),
                                          db_path=db_path):
        if candidate_key(existing) == key:
            return existing
    return None


def emit_candidate(cand, db_path=None, dry_run=False):
    """Store a candidate (or just echo it on dry runs). Returns the id or None.

    Idempotent (issue #32): if a candidate with the same natural key
    (:func:`candidate_key`) already exists, emission is a no-op returning
    the EXISTING id — never a duplicate row, and never an overwrite (the
    stored row may already have advanced status / hub_url / description
    downstream; a re-scan must not clobber that progress). This holds even
    when the incoming candidate carries DIFFERENT event-specific metrics or
    description for the same key: the new payload is discarded and the
    stored row wins.

    Stub descriptions (the detectors' one-liners) are upgraded to the full
    digest-quality text via clipsmith.describer at emit time; hand-written
    descriptions pass through untouched.

    ``dry_run=True`` prints the candidate JSON to stdout without touching the
    store (not even reading it — the dedup probe would create the DB file) —
    used by detector CLIs for validation runs.
    """
    from clipsmith import describer  # late import: describer imports store
    if not dry_run:
        existing = find_existing_candidate(cand, db_path=db_path)
        if existing is not None:
            log.info("duplicate candidate (key=%r): no-op, keeping existing "
                     "id %s", candidate_key(cand), existing["id"])
            return existing["id"]
    if describer.looks_like_stub(cand.get("description", "")):
        cand = dict(cand)
        cand["description"] = describer.describe(cand)
    if dry_run:
        print(json.dumps(cand, indent=2))
        return None
    return store.add_candidate(cand, db_path=db_path)
