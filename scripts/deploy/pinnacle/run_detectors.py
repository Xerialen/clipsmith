#!/usr/bin/env python3
"""clipsmith detection-stage orchestrator (issue #21, deploy-side).

Consumes the watcher's durable handoff ``state/pending-detection.jsonl``
(see clipsmith/watcher.py): for every pending demo, runs the four Phase-1
detectors (QUAD RUNNER, TEAM KILLER, PENT GRIND, PENT RUNNER — the COMEBACK
detector is Phase-5 and not on main) as subprocesses against mvd-api, then a
hub_links resolution pass over candidates still missing a hub URL.

Retry bookkeeping per (sha, detector): the runner keeps its OWN ledger at
``state/detect-ledger.json`` ({sha: {detector: iso-ts}}) so that when one
detector fails, the next run skips the detectors that already succeeded for
that sha. A sha leaves pending-detection.jsonl (and the ledger) only when all
four detectors have succeeded for it.

IMPORTANT — the ledger is a retry/abandonment OPTIMIZATION, not a correctness
mechanism. It is written after each demo, so a crash between a detector's
emit and the ledger write still replays that detector on the next run.
Correctness against double-emits comes from ``emit_candidate`` being
natural-key idempotent (PR #39: duplicate (category, sha, pov, window) is a
no-op returning the existing id). install.sh probes for that behavior at
install time and refuses to enable clipsmith-detect.timer without it.

This file deliberately lives under scripts/deploy/ — it orchestrates the
clipsmith CLIs (watcher/detectors/store/hub_links) without modifying them.

Exit codes: 0 all pending demos fully detected; 1 any detector failed
(entries kept for retry). hub_links misses (exit 3 = sha not on hub yet) are
informational; hub failures are warnings — link resolution retries on every
run and the digest renders an explicit "missing" note.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DETECTORS = (
    "clipsmith.detectors.quad_runner",
    "clipsmith.detectors.team_killer",
    "clipsmith.detectors.pent_grind",
    "clipsmith.detectors.pent_runner",
)
DETECTOR_TIMEOUT_S = 600
HUB_EXIT_NOT_FOUND = 3
# A demo that keeps failing (typically mvd-api 404: the demo never reached
# hub.quakeworld.nu, so sha-based analysis is impossible) is abandoned after
# this many failed runs instead of alerting forever. Cadence is 4 runs/week
# (Tue/Thu/Sat detection + the Monday pre-digest sweep), so 8 runs ≈ 2 weeks.
# Abandonment is logged loudly and recorded in the ledger.
MAX_ATTEMPTS = 8
ATTEMPTS_KEY = "__attempts"
ABANDONED_KEY = "__abandoned_at"


def log(*args):
    print("[run_detectors]", *args, flush=True)


def read_pending(state_dir):
    """Read pending-detection.jsonl, quarantining malformed lines.

    A torn/truncated line (e.g. from a crash mid-write) must not abort
    processing of subsequent valid lines.  Bad lines are moved to
    pending-detection-quarantine.jsonl with a loud log so they can be
    inspected.  Returns only the parseable entries.
    """
    path = Path(state_dir) / "pending-detection.jsonl"
    if not path.exists():
        return []
    entries = []
    bad_lines = []
    with open(path, encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                log(f"WARN: malformed line {lineno} in pending-detection.jsonl "
                    f"(skipping, quarantined): {exc}: {line[:120]!r}")
                bad_lines.append(raw_line)
    if bad_lines:
        q_path = Path(state_dir) / "pending-detection-quarantine.jsonl"
        with open(q_path, "a", encoding="utf-8") as qf:
            qf.writelines(bad_lines)
        log(f"WARN: {len(bad_lines)} malformed line(s) appended to {q_path}")
    return entries


def write_pending(state_dir, entries):
    """Atomically rewrite pending-detection.jsonl with the remaining entries."""
    path = Path(state_dir) / "pending-detection.jsonl"
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def ledger_path(state_dir):
    return Path(state_dir) / "detect-ledger.json"


def read_ledger(state_dir):
    path = ledger_path(state_dir)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_ledger(state_dir, ledger):
    path = ledger_path(state_dir)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def run_cli(repo, argv, timeout=DETECTOR_TIMEOUT_S):
    """Run a clipsmith CLI as a subprocess from the deploy clone root."""
    return subprocess.run(
        [sys.executable, "-m", *argv],
        cwd=repo, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def detect_sha(repo, sha, db, api_url, done):
    """Run the not-yet-done detectors for one sha. Returns (newly_done, failed)."""
    newly_done, failed = [], []
    for mod in DETECTORS:
        name = mod.rsplit(".", 1)[1]
        if name in done:  # bookkeeping keys (__attempts) never match
            continue
        try:
            proc = run_cli(repo, (mod, "--sha", sha, "--db", db,
                                  "--api-url", api_url))
        except subprocess.TimeoutExpired:
            log(f"FAIL {name} sha={sha}: timeout after {DETECTOR_TIMEOUT_S}s")
            failed.append(name)
            continue
        tail = (proc.stdout or "").strip().splitlines()[-3:]
        if proc.returncode == 0:
            log(f"ok   {name} sha={sha}: " + (" | ".join(tail) or "(no output)"))
            newly_done.append(name)
        else:
            log(f"FAIL {name} sha={sha} rc={proc.returncode}: "
                + (" | ".join(tail) or "(no output)"))
            failed.append(name)
    return newly_done, failed


def resolve_hub_links(repo, db):
    """hub_links pass over status=candidate rows with empty hub_url.

    Returns the number of GENUINE failures (exit 3 'not on hub yet' is an
    expected miss, retried next run). Never raises.
    """
    env = dict(os.environ, CLIPSMITH_DB=db)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "clipsmith.store", "list",
             "--status", "candidate"],
            cwd=repo, env=env, timeout=120,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except subprocess.TimeoutExpired:
        log("WARN hub-links: store list timed out")
        return 1
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
        detail = f": {tail[0]}" if tail else ""
        log(f"WARN hub-links: store list failed rc={proc.returncode}{detail}")
        return 1

    missing = []
    for lineno, line in enumerate((proc.stdout or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            cand = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"WARN hub-links: malformed store-list line {lineno} "
                f"(skipping): {exc}: {line[:120]!r}")
            continue
        if not isinstance(cand, dict):
            log(f"WARN hub-links: non-object store-list line {lineno} "
                f"(skipping): {line[:120]!r}")
            continue
        if not cand.get("hub_url"):
            cand_id = cand.get("id")
            if not cand_id:
                log(f"WARN hub-links: missing candidate id on store-list "
                    f"line {lineno} (skipping)")
                continue
            missing.append(cand_id)
    if not missing:
        log("hub-links: nothing to resolve")
        return 0

    failures = 0
    for cand_id in missing:
        try:
            proc = run_cli(repo, ("clipsmith.hub_links",
                                  "--candidate", cand_id, "--db", db),
                           timeout=120)
        except subprocess.TimeoutExpired:
            log(f"WARN hub-links {cand_id}: timeout")
            failures += 1
            continue
        tail = (proc.stdout or "").strip().splitlines()[-1:] or ["(no output)"]
        if proc.returncode == 0:
            log(f"hub-links ok {cand_id}: {tail[0]}")
        elif proc.returncode == HUB_EXIT_NOT_FOUND:
            log(f"hub-links miss {cand_id}: not on hub yet (retry next run)")
        else:
            log(f"WARN hub-links {cand_id} rc={proc.returncode}: {tail[0]}")
            failures += 1
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="deploy clone root")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8080")
    args = parser.parse_args(argv)

    pending = read_pending(args.state_dir)
    ledger = read_ledger(args.state_dir)
    log(f"{len(pending)} demo(s) pending detection")

    remaining, any_failed = [], False
    for i, entry in enumerate(pending):
        sha = entry["sha"]
        done = ledger.get(sha, {})
        newly_done, failed = detect_sha(args.repo, sha, args.db,
                                        args.api_url, done)
        now = datetime.now(timezone.utc).isoformat()
        for name in newly_done:
            done[name] = now
        if failed:
            done[ATTEMPTS_KEY] = done.get(ATTEMPTS_KEY, 0) + 1
            ledger[sha] = done           # keep partial progress
            if done[ATTEMPTS_KEY] >= MAX_ATTEMPTS:
                done[ABANDONED_KEY] = now
                log(f"ABANDONED sha={sha} after {done[ATTEMPTS_KEY]} failed "
                    f"runs (still failing: {', '.join(failed)}) — dropping "
                    f"from pending; ledger keeps the record")
            else:
                any_failed = True
                remaining.append(entry)  # retry the failed detectors next run
            # Persist progress after every demo so a hard kill (wsl
            # --shutdown) loses at most one detector invocation worth of
            # bookkeeping. Ledger first: the partial-progress record must be
            # durable while the sha is still pending.
            write_ledger(args.state_dir, ledger)
            write_pending(args.state_dir, remaining + pending[i + 1:])
        else:
            # Fully detected: leave pending AND the ledger. Ordering matters
            # (review finding): make the pending removal durable BEFORE
            # dropping the ledger entry — a crash between the two writes
            # then leaves a harmless stale ledger entry instead of a pending
            # sha with no ledger, which would re-run all four detectors from
            # scratch next run (double-emit without #39 idempotency).
            write_pending(args.state_dir, remaining + pending[i + 1:])
            ledger.pop(sha, None)
            write_ledger(args.state_dir, ledger)

    hub_failures = resolve_hub_links(args.repo, args.db)
    if hub_failures:
        log(f"WARN: {hub_failures} hub-link resolution failure(s) — non-fatal")

    log(f"done: {len(pending) - len(remaining)}/{len(pending)} demo(s) fully "
        f"detected, {len(remaining)} kept for retry")
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
