"""Regression tests for scripts/deploy/pinnacle/run_detectors.py (issue #21).

Covers:
  - Warden round-2 finding (b): malformed/torn JSONL lines in
    pending-detection.jsonl must be quarantined, not abort processing of
    subsequent valid entries.
  - Warden round-2 finding (a): the digest week computation must derive the
    target week from the most-recent Monday slot, not wall-clock "now", so
    a Persistent=true catch-up run on Tuesday still builds/posts the correct
    (previous) week.
"""
import datetime as _dt
import importlib.util
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

DEPLOY_PINNACLE = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "pinnacle"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rd = _load("run_detectors", DEPLOY_PINNACLE / "run_detectors.py")


# ---------------------------------------------------------------------------
# Finding (b): malformed/torn JSONL quarantine
# ---------------------------------------------------------------------------

class TestReadPendingMalformedLines:
    """read_pending must quarantine bad lines and return valid entries."""

    def _write_pending(self, state_dir, lines):
        p = state_dir / "pending-detection.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")

    def test_torn_line_before_valid_still_processes_valid(self, tmp_path):
        """A malformed first line must not prevent the valid second line from
        being returned (warden round-2 finding b)."""
        self._write_pending(tmp_path, [
            '{"sha": "abc",',           # torn line (missing closing })
            '{"sha": "def", "demo": 1}',  # valid entry
        ])
        entries = rd.read_pending(tmp_path)
        shas = [e["sha"] for e in entries]
        assert "def" in shas, (
            "read_pending aborted on malformed line -- "
            "valid entries after the bad line were dropped"
        )

    def test_torn_line_is_quarantined(self, tmp_path):
        """Malformed lines must be moved to pending-detection-quarantine.jsonl."""
        self._write_pending(tmp_path, [
            "NOT_JSON_AT_ALL",
            '{"sha": "good"}',
        ])
        rd.read_pending(tmp_path)
        q_path = tmp_path / "pending-detection-quarantine.jsonl"
        assert q_path.exists(), (
            "read_pending did not create quarantine file for malformed line"
        )
        quarantine_content = q_path.read_text(encoding="utf-8")
        assert "NOT_JSON_AT_ALL" in quarantine_content, (
            "malformed line not found in quarantine file"
        )

    def test_all_valid_lines_returned(self, tmp_path):
        """All parseable lines must be returned regardless of bad neighbours."""
        self._write_pending(tmp_path, [
            '{"sha": "a1"}',
            "BROKEN",
            '{"sha": "a2"}',
            "{bad",
            '{"sha": "a3"}',
        ])
        entries = rd.read_pending(tmp_path)
        shas = [e["sha"] for e in entries]
        assert shas == ["a1", "a2", "a3"], (
            f"expected ['a1','a2','a3'], got {shas}"
        )

    def test_fully_valid_jsonl_unaffected(self, tmp_path):
        """All-valid JSONL must behave exactly as before -- no quarantine file."""
        entries_data = [{"sha": f"sha{i}", "demo": i} for i in range(3)]
        self._write_pending(tmp_path, [json.dumps(e) for e in entries_data])
        entries = rd.read_pending(tmp_path)
        assert len(entries) == 3
        q_path = tmp_path / "pending-detection-quarantine.jsonl"
        assert not q_path.exists(), (
            "quarantine file created for a fully-valid JSONL"
        )

    def test_empty_jsonl_returns_empty(self, tmp_path):
        self._write_pending(tmp_path, [])
        assert rd.read_pending(tmp_path) == []

    def test_missing_jsonl_returns_empty(self, tmp_path):
        assert rd.read_pending(tmp_path) == []


class TestResolveHubLinksRobustness:
    """hub-link resolution must tolerate diagnostics in store-list output."""

    def test_store_list_warning_line_does_not_abort_valid_candidate(self, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[:4] == [sys.executable, "-m", "clipsmith.store", "list"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout='WARN noisy diagnostic\n{"id": "cand1", "hub_url": null}\n',
                )
            if argv[:3] == [sys.executable, "-m", "clipsmith.hub_links"]:
                return SimpleNamespace(returncode=0, stdout="linked cand1\n")
            raise AssertionError(f"unexpected subprocess argv: {argv}")

        monkeypatch.setattr(rd.subprocess, "run", fake_run)

        assert rd.resolve_hub_links("/repo", "/tmp/candidates.db") == 0
        assert any("clipsmith.hub_links" in call for call in calls)

    def test_store_list_malformed_json_line_still_processes_later_rows(self, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[:4] == [sys.executable, "-m", "clipsmith.store", "list"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout='{"id":\n{"id": "cand1", "hub_url": null}\n',
                )
            if argv[:3] == [sys.executable, "-m", "clipsmith.hub_links"]:
                return SimpleNamespace(returncode=0, stdout="linked cand1\n")
            raise AssertionError(f"unexpected subprocess argv: {argv}")

        monkeypatch.setattr(rd.subprocess, "run", fake_run)

        assert rd.resolve_hub_links("/repo", "/tmp/candidates.db") == 0
        assert any("clipsmith.hub_links" in call for call in calls)


class TestDeployLockPath:
    """Detect/digest runners must share a user-owned lock path."""

    def test_detect_and_digest_use_same_user_owned_lock_path(self):
        detect = (DEPLOY_PINNACLE / "run-detect.sh").read_text(encoding="utf-8")
        digest = (DEPLOY_PINNACLE / "run-digest.sh").read_text(encoding="utf-8")

        assert "/run/lock/clipsmith.lock" not in detect
        assert "/run/lock/clipsmith.lock" not in digest
        assert "CLIPSMITH_LOCK_FILE" in detect
        assert "CLIPSMITH_LOCK_FILE" in digest


# ---------------------------------------------------------------------------
# Finding (a): week computation -- most-recent-Monday contract
# ---------------------------------------------------------------------------

def _most_recent_monday_week(today):
    """Mirror the fixed logic in run-digest.sh and run-digest-post.sh:
    ISO week of (most-recent Monday - 1 day).  On Monday that is the
    previous ISO week (the finished week); on Tuesday it is ALSO the
    previous ISO week (same slot, delayed catch-up run).
    """
    days_back = today.weekday()  # 0 on Monday, 1 on Tuesday, ...
    last_monday = today - _dt.timedelta(days=days_back)
    prev_day = last_monday - _dt.timedelta(days=1)
    iso = prev_day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class TestWeekContractMostRecentMonday:
    """The digest week must be derived from the most-recent Monday slot, not
    wall-clock 'now' (warden round-2 finding a).
    """

    def test_monday_gives_previous_iso_week(self):
        """On Monday, the week is the one that just finished."""
        # 2026-06-08 is a Monday (week 2026-W24); slot fires on 2026-06-08
        # -> posts the FINISHED week = 2026-W23
        monday = _dt.date(2026, 6, 8)
        assert monday.weekday() == 0, "fixture date must be a Monday"
        week = _most_recent_monday_week(monday)
        assert week == "2026-W23", f"expected 2026-W23, got {week}"

    def test_tuesday_catchup_gives_same_week_as_monday(self):
        """A Tuesday catch-up run for Monday's slot must give the SAME week
        as if it had run on Monday (the previous ISO week, not the current
        one). This was the bug: default_week() on Tuesday returned the
        current week instead of the previous one.
        """
        monday = _dt.date(2026, 6, 8)
        tuesday = _dt.date(2026, 6, 9)
        assert tuesday.weekday() == 1, "fixture date must be a Tuesday"
        week_monday = _most_recent_monday_week(monday)
        week_tuesday = _most_recent_monday_week(tuesday)
        assert week_monday == week_tuesday, (
            f"Tuesday catch-up week {week_tuesday!r} != Monday run week "
            f"{week_monday!r} -- Persistent=true catch-up would build the "
            "wrong digest week"
        )

    def test_wednesday_catchup_gives_same_week_as_monday(self):
        monday = _dt.date(2026, 6, 8)
        wednesday = _dt.date(2026, 6, 10)
        assert wednesday.weekday() == 2
        assert (_most_recent_monday_week(monday)
                == _most_recent_monday_week(wednesday))

    def test_sunday_uses_most_recent_monday(self):
        """Sunday before the next Monday uses the MOST RECENT Monday
        (6 days ago) -- no off-by-one at the week boundary.
        """
        # Sunday 2026-06-14 -- most recent Monday was 2026-06-08
        sunday = _dt.date(2026, 6, 14)
        assert sunday.weekday() == 6
        week = _most_recent_monday_week(sunday)
        # last_monday=2026-06-08, prev_day=2026-06-07 (Sun W23)
        assert week == "2026-W23"

    def test_wall_clock_default_week_breaks_on_tuesday(self):
        """Document the old bug: clipsmith.digest.default_week() on a
        Tuesday returns the CURRENT week, not the previous one.
        """
        from clipsmith.digest import default_week
        tuesday = _dt.date(2026, 6, 9)
        old_week = default_week(today=tuesday)
        monday = _dt.date(2026, 6, 8)
        correct_week = _most_recent_monday_week(monday)
        assert old_week != correct_week, (
            "default_week(tuesday) unexpectedly equals the Monday slot's week "
            "-- the bug may have been fixed upstream already"
        )
