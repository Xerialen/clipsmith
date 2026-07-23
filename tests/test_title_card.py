"""Pure-logic tests for the title-card generator (issue #23).

No Playwright, no ffmpeg, no Drive writes — only the deterministic helpers in
scripts/assembly/title_card/make_title_card.py: slugs, category->style mapping,
the single-timing-source pulse/sound math, output naming, and the
sound_config.json contract (the #14 real-sound drop-in point).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

TITLE_CARD_DIR = Path(__file__).resolve().parents[1] / "scripts" / "assembly" / "title_card"


def _load_module():
    sys.path.insert(0, str(TITLE_CARD_DIR))
    spec = importlib.util.spec_from_file_location(
        "make_title_card", TITLE_CARD_DIR / "make_title_card.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mtc = _load_module()


# ----------------------------------------------------------------- slugify
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("kippo (FU)", "kippo-fu"),
        ("QUAD RUNNER", "quad-runner"),
        ("Spectator", "spectator"),
        ("]SR[grisling", "sr-grisling"),
        ("...", "x"),  # never an empty slug
    ],
)
def test_slugify(raw, expected):
    assert mtc.slugify(raw) == expected


# ----------------------------------------------------------- category modes
@pytest.mark.parametrize(
    "category,mode",
    [
        ("QUAD RUNNER", "quad"),
        ("quad runner", "quad"),  # case-insensitive
        (" PENT RUNNER ", "pent"),  # whitespace-tolerant
        ("TEAM KILLER", "plain"),  # explicitly plain per spec
        ("PENT GRIND", "plain"),
        ("SOMETHING NEW", "plain"),  # unknown categories degrade to plain
    ],
)
def test_category_mode(category, mode):
    assert mtc.category_mode(category) == mode


# ------------------------------------------------- timing: one shared source
def test_compute_timing_defaults():
    t = mtc.compute_timing()
    assert t["total_frames"] == 105  # 3.5 s @ 30 fps
    assert t["pulse_peak_frame"] == 36  # 1.2 s @ 30 fps
    assert t["delay_ms"] == 1200  # transient at file start -> delay == peak

    # the invariant the whole sync rests on: peak frame / fps == delay
    assert t["pulse_peak_frame"] / 30 == pytest.approx(t["delay_ms"] / 1000)


def test_compute_timing_transient_offset_shifts_delay_not_peak():
    # a real game sound with 0.3 s of lead-in before its attack (#14 scenario)
    t = mtc.compute_timing(transient_offset_s=0.3)
    assert t["pulse_peak_frame"] == 36  # visual peak unchanged
    assert t["delay_ms"] == 900  # file starts earlier so attack hits 1.2 s


def test_compute_timing_rejects_offset_beyond_peak():
    with pytest.raises(ValueError):
        mtc.compute_timing(transient_offset_s=5.0)


# ------------------------------------------------------------ output naming
def test_output_basename():
    assert (
        mtc.output_basename("QUAD RUNNER", "kippo (FU)", 24)
        == "week24_quad-runner_kippo-fu"
    )
    assert mtc.output_basename("PENT GRIND", "paniagua", 7).startswith("week07_")


# --------------------------------------------- sound config (#14 drop-in)
def test_sound_config_has_quad_and_pent_placeholders():
    cfg = json.loads((TITLE_CARD_DIR / "sound_config.json").read_text(encoding="utf-8"))
    for cat in ("QUAD RUNNER", "PENT RUNNER"):
        entry = cfg["sounds"][cat]
        wav = TITLE_CARD_DIR / entry["path"]
        assert wav.is_file(), f"missing stand-in: {wav}"
        assert "PLACEHOLDER" in wav.name  # explicit until #14 delivers
        assert wav.stat().st_size < 200_000  # repo-size rule
        assert entry["transient_offset_s"] == 0.0  # synthesized attack at t=0


def test_load_sound_for_resolves_relative_paths():
    snd = mtc.load_sound_for("QUAD RUNNER")
    assert snd is not None
    path, offset = snd
    assert path.is_absolute() and path.is_file()
    assert offset == 0.0


def test_load_sound_for_plain_categories_is_none():
    assert mtc.load_sound_for("TEAM KILLER") is None
    assert mtc.load_sound_for("PENT GRIND") is None


# ------------------------------------------------------- font readiness gate
def test_title_card_html_fails_closed_on_required_font_errors():
    html = (TITLE_CARD_DIR / "card_html.py").read_text(encoding="utf-8")

    assert "window.__fontError" in html
    assert "document.fonts.check" in html
    assert "window.__ready=true" in html
    assert "catch(e){}" not in html


def test_title_card_renderer_checks_font_error_before_screenshots():
    renderer = (TITLE_CARD_DIR / "make_title_card.py").read_text(encoding="utf-8")

    assert "window.__ready === true || window.__fontError" in renderer
    assert "window.__fontError || null" in renderer
    assert "font load failed" in renderer
