"""COME-BACK detector evaluation gate (issue #30 point 6).

Runs the detector across the labeled set in tests/fixtures/comeback_eval.json
(5 known comebacks + 5 controls, ground truth documented in the fixture) and
reports the confusion matrix + precision/recall. The issue's gate: the
detector flags every known comeback and none of the controls; the digest
COMEBACK flag stays off until the owner reviews the reported precision.

OPT-IN — needs a live mvd-api with the fixture demos cached (cold sha-loads
404; warm them via POST /v1/demos/gameId:<hub_game_id>, see the fixture
description). Excluded from the default suite:

    CLIPSMITH_EVAL=1 python -m pytest tests/test_come_back_eval.py -s

The confusion matrix prints regardless of pass/fail (use -s); a gate
failure's message carries the full matrix too, so the precision evidence
survives into CI logs.
"""
import json
import os
from pathlib import Path

import pytest

from clipsmith.detectors import come_back, common

FIXTURE = Path(__file__).parent / "fixtures" / "comeback_eval.json"

pytestmark = pytest.mark.skipif(
    os.environ.get("CLIPSMITH_EVAL") != "1",
    reason="live eval gate: set CLIPSMITH_EVAL=1 with mvd-api running "
           "and the fixture demos cached",
)


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_well_formed():
    """Offline sanity on the labeled set itself (runs only in eval mode to
    keep the default suite's scope unchanged; the JSON is also exercised
    implicitly by the eval)."""
    fx = load_fixture()
    demos = fx["demos"]
    labels = {d["label"] for d in demos}
    assert labels == {"comeback", "control"}
    positives = [d for d in demos if d["label"] == "comeback"]
    controls = [d for d in demos if d["label"] == "control"]
    assert 3 <= len(positives) <= 5      # issue #30: 3-5 known comebacks
    assert len(controls) >= 5            # ~5 controls
    assert len({d["sha"] for d in demos}) == len(demos)
    for d in demos:
        assert len(d["sha"]) == 64
        assert d["evidence"]


def test_eval_confusion_matrix():
    fx = load_fixture()
    client = common.MvdApiClient()

    rows, tp, fn, fp, tn = [], [], [], [], []
    for d in fx["demos"]:
        result = come_back.analyze(client, d["sha"])
        flagged = bool(result["candidates"])
        top = (result["candidates"][0] if flagged else None)
        rows.append((d, flagged, top))
        bucket = {("comeback", True): tp, ("comeback", False): fn,
                  ("control", True): fp, ("control", False): tn}
        bucket[(d["label"], flagged)].append(d)

    lines = ["", "COME-BACK eval (defaults) — per demo:"]
    for d, flagged, top in rows:
        detail = ""
        if top is not None:
            m = top["metrics"]
            detail = (f" top: {top['pov_player']['name']} "
                      f"{m['carrier_kills']}x carrier, "
                      f"lead_at_worst {m['lead_at_worst']}, "
                      f"score {top['score']:g}")
        lines.append(f"  {d['sha'][:8]} {d['map']:<12} "
                     f"label={d['label']:<8} flagged={flagged}{detail}")
    precision = len(tp) / (len(tp) + len(fp)) if (tp or fp) else float("nan")
    recall = len(tp) / (len(tp) + len(fn)) if (tp or fn) else float("nan")
    lines += [
        f"confusion matrix: TP={len(tp)} FN={len(fn)} "
        f"FP={len(fp)} TN={len(tn)}",
        f"precision={precision:.2f} recall={recall:.2f}",
    ]
    report = "\n".join(lines)
    print(report)

    # The issue #30 gate: every known comeback flagged, no control flagged.
    assert not fn and not fp, report
