"""Regression tests for render failure recovery and media probing.

P1: durable render-failure record (candidate stays 'selected', failure logged)
P1: worker traps EXIT/INT/TERM, salvages failure logs, staging never leaks
P2: probe_video normalizes near-integer fps, rejects fractional rates
"""
import importlib.util
import json
from pathlib import Path

import pytest

from clipsmith import render as rc

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
asm = _load("asm_fixes",
            REPO_ROOT / "scripts" / "assembly" / "resolve_assembly" / "assemble.py")


def test_record_render_failure_appends_jsonl(tmp_path, monkeypatch):
    log = tmp_path / "render-failures.jsonl"
    monkeypatch.setattr(rc, "FAILURES_LOG", log)
    rc.record_render_failure("cand-1", 14, "engine died", db_path="x.db")
    rc.record_render_failure("cand-1", 18, "blackdetect")
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    assert [e["exit_code"] for e in lines] == [14, 18]
    assert lines[0]["candidate"] == "cand-1"
    assert lines[0]["error"] == "engine died"
    assert "ts" in lines[0]


def test_orchestrator_records_failure_before_exit():
    src = (REPO_ROOT / "clipsmith" / "render.py").read_text()
    # both failure exits after the worker run must record durably first
    assert src.count("record_render_failure(") >= 3  # def + 2 call sites


def test_worker_traps_exit_and_salvages_logs():
    text = (REPO_ROOT / "scripts" / "render" / "render_worker.sh").read_text()
    assert "trap on_exit EXIT INT TERM" in text
    on_exit_body = text.split("on_exit()")[1].split("\n}")[0]
    assert "KEEP_STAGING" in on_exit_body          # staging removal lives in the trap
    assert "failed-$JOB_ID-logs" in on_exit_body   # diagnostics salvaged on failure


def test_probe_video_returns_integer_fps(monkeypatch):
    def fake_ffprobe(path):
        return {"format": {"duration": "35.0"},
                "streams": [{"codec_type": "video", "r_frame_rate": "60/1",
                             "width": 1920, "height": 1080}]}
    monkeypatch.setattr(asm, "ffprobe", fake_ffprobe)
    fps = asm.probe_video("x.mov")["fps"]
    assert fps == 60 and isinstance(fps, int)


@pytest.mark.parametrize("rate", ["60000/1001", "25/2"])  # NTSC 59.94, 12.5
def test_probe_video_rejects_fractional_fps(monkeypatch, rate):
    def fake_ffprobe(path):
        return {"format": {"duration": "35.0"},
                "streams": [{"codec_type": "video", "r_frame_rate": rate,
                             "width": 1920, "height": 1080}]}
    monkeypatch.setattr(asm, "ffprobe", fake_ffprobe)
    with pytest.raises(ValueError, match="fractional frame rate"):
        asm.probe_video("x.mov")
