"""Synthesize PLACEHOLDER pickup-sound stand-ins for the title cards (ffmpeg).

HITL #14 (owner-provided real quad/pent pickup sounds from the game files) is
not done yet; these are brand-plausible, licensing-free stand-ins following the
ffmpeg SFX synthesis pattern proven in scripts/branding/assemble_branded.sh
(layered sub-sines plus shaped noise impacts).

Drop-in point for the real sounds: scripts/assembly/title_card/sound_config.json
— point the category's "path" at the real file (and set "transient_offset_s" if
its attack isn't at t=0). No code change needed.

Both stand-ins put their transient (sharp attack) at t=0 — the pulse animation
peak is aligned to the sound's start, so keep that property if regenerating.

Output: mono s16 48 kHz WAV, ~1s (<100 KB each, fine to track in the repo).
Regenerate with:  python scripts/assembly/title_card/synth_sounds.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"


def _run(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True)


def synth_quad(out: Path) -> None:
    """Quad pickup stand-in: bright rising power-up shimmer over a tight impact."""
    _run([
        "ffmpeg", "-y",
        # impact transient at t=0 (brown noise burst)
        "-f", "lavfi", "-i", "anoisesrc=d=0.07:color=brown:amplitude=0.8",
        # rising two-voice shimmer with exponential decay (the "energy up" feel)
        "-f", "lavfi", "-i",
        "aevalsrc=exp(-3.2*t)*(0.5*sin(2*PI*(380+1500*t)*t)+0.35*sin(2*PI*(570+2250*t)*t)):d=0.95:s=48000",
        # sub thump under the attack
        "-f", "lavfi", "-i", "sine=frequency=72:duration=0.22",
        "-filter_complex",
        "[0]lowpass=f=1400,volume=1.1[i];"
        "[1]highpass=f=250,volume=0.9[s];"
        "[2]afade=t=out:st=0.02:d=0.2:curve=exp,volume=0.9[b];"
        "[i][s][b]amix=inputs=3:normalize=0,afade=t=out:st=0.7:d=0.25,"
        "volume=2.4,alimiter=limit=0.95",
        "-ar", "48000", "-ac", "1", "-sample_fmt", "s16", "-t", "0.95", str(out),
    ])


def synth_pent(out: Path) -> None:
    """Pent pickup stand-in: ominous detuned low drone with a heavier impact."""
    _run([
        "ffmpeg", "-y",
        # heavier, darker impact transient at t=0
        "-f", "lavfi", "-i", "anoisesrc=d=0.1:color=brown:amplitude=0.9",
        # detuned descending lows (menace)
        "-f", "lavfi", "-i",
        "aevalsrc=exp(-2.2*t)*(0.55*sin(2*PI*(112-38*t)*t)+0.4*sin(2*PI*56*t)+0.3*sin(2*PI*(168-60*t)*t)):d=1.05:s=48000",
        "-filter_complex",
        "[0]lowpass=f=500,volume=1.3[i];"
        "[1]aphaser=in_gain=0.7:out_gain=0.9:delay=2:decay=0.4:speed=0.8,volume=1.0[d];"
        "[i][d]amix=inputs=2:normalize=0,afade=t=out:st=0.78:d=0.27,"
        "volume=2.6,alimiter=limit=0.95",
        "-ar", "48000", "-ac", "1", "-sample_fmt", "s16", "-t", "1.05", str(out),
    ])


def main() -> None:
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    quad = SOUNDS_DIR / "PLACEHOLDER_quad_pickup.wav"
    pent = SOUNDS_DIR / "PLACEHOLDER_pent_pickup.wav"
    synth_quad(quad)
    synth_pent(pent)
    for p in (quad, pent):
        print(f"{p.name}  {p.stat().st_size} bytes")


if __name__ == "__main__":
    main()
