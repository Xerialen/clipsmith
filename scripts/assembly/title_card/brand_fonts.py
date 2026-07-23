"""Extract the OneMoreQUAD brand fonts from a brand-kit OBS scene bundle.

Python port of the proven ``scripts/branding/pick_fonts2.mjs`` (2026-06-10 PoC).
The scene HTML embeds fonts in a ``<script type="__bundler/manifest">`` JSON
blob keyed by uuid; the @font-face CSS lives in ``<script type="__bundler/template">``.

Hard-won implementation gotcha: **Nightrunner is a gzip-compressed
TrueType** (``compressed: true``, magic ``00 01 00 00`` after gunzip) — it must
be gunzipped and embedded as ``format('truetype')``; embedding it raw as woff2
silently fails (FontFace status "error") and canvas falls back to serif.

Brand assets are CONSUMED from ``Xerialen/onemorequad-brand`` — never copied
into this repo. Extracted fonts are cached in a local scratch dir.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import tempfile
import zlib
from pathlib import Path

# Brand kit checkout. Any scene carries the full font set in its bundle
# manifest; Starting Soon is the one build.sh uses.
_brand_scenes_dir = os.environ.get("CLIPSMITH_BRAND_SCENES_DIR")
DEFAULT_BRAND_SCENE = (
    Path(_brand_scenes_dir) / "QUAD RUNNER - Starting Soon (1920x1080).html"
    if _brand_scenes_dir else None
)

# family, weight, style, cache filename, css format
WANTED_FONTS = [
    ("Nightrunner", "700", "normal", "Nightrunner-700-normal.ttf", "truetype"),
    ("Chakra Petch", "700", "italic", "ChakraPetch-700-italic.woff2", "woff2"),
    ("Chakra Petch", "700", "normal", "ChakraPetch-700-normal.woff2", "woff2"),
    ("Share Tech Mono", "400", "normal", "ShareTechMono-400-normal.woff2", "woff2"),
]


def default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "clipsmith" / "brand_fonts"


def _grab_bundler_block(html: str, kind: str) -> str | None:
    opener = f'<script type="__bundler/{kind}">'
    i = html.find(opener)
    if i < 0:
        return None
    j = html.find("</script>", i + len(opener))
    return html[i + len(opener) : j]


def _gunzip_maybe(raw: bytes) -> bytes:
    try:
        return gzip.decompress(raw)
    except OSError:
        return zlib.decompress(raw)


def extract_fonts(scene_html_path: Path | None = None, cache_dir: Path | None = None) -> dict[str, Path]:
    """Extract WANTED_FONTS from the brand scene bundle into cache_dir.

    Returns a dict {cache filename -> path}. Cached results are reused.
    """
    selected_scene = scene_html_path or DEFAULT_BRAND_SCENE
    if selected_scene is None:
        raise ValueError(
            "set CLIPSMITH_BRAND_SCENES_DIR or pass scene_html_path"
        )
    scene = Path(selected_scene)
    cache = Path(cache_dir or default_cache_dir())
    cache.mkdir(parents=True, exist_ok=True)

    out = {fn: cache / fn for _, _, _, fn, _ in WANTED_FONTS}
    if all(p.is_file() and p.stat().st_size > 0 for p in out.values()):
        return out

    html = scene.read_text(encoding="utf-8", errors="replace")
    css = _grab_bundler_block(html, "template")
    if css is None:
        raise RuntimeError(f"no __bundler/template block in {scene}")
    try:  # the template block may be a JSON-encoded string
        decoded = json.loads(css)
        css = decoded if isinstance(decoded, str) else json.dumps(decoded)
    except (json.JSONDecodeError, ValueError):
        pass
    manifest = json.loads(_grab_bundler_block(html, "manifest") or "{}")

    face_map: dict[str, str] = {}
    for face in re.findall(r"@font-face\s*\{[^}]*\}", css):
        fam = re.search(r"font-family:\s*['\"]?([^;'\"}]+)", face)
        uuid = re.search(r"[0-9a-f-]{36}", face)
        weight = re.search(r"font-weight:\s*([^;}]+)", face)
        style = re.search(r"font-style:\s*([^;}]+)", face)
        if fam and uuid:
            key = "|".join(
                [fam.group(1).strip(), (weight.group(1).strip() if weight else "400"),
                 (style.group(1).strip() if style else "normal")]
            )
            face_map[key] = uuid.group(0)

    for fam, weight, style, fname, _fmt in WANTED_FONTS:
        key = f"{fam}|{weight}|{style}"
        if key not in face_map:  # fall back to any weight/style of the family
            key = next((k for k in face_map if k.startswith(fam + "|")), key)
        uuid = face_map.get(key)
        entry = manifest.get(uuid) if uuid else None
        if not entry:
            raise RuntimeError(f"font not found in brand bundle: {fam} ({weight} {style})")
        raw = base64.b64decode(re.sub(r"^data:[^,]*,", "", entry.get("data", "")))
        if entry.get("compressed"):
            raw = _gunzip_maybe(raw)
        (cache / fname).write_bytes(raw)

    return out


def font_face_css(fonts: dict[str, Path]) -> str:
    """Build the @font-face CSS with fonts inlined as base64 (canvas-safe)."""

    def b64(fname: str) -> str:
        return base64.b64encode(fonts[fname].read_bytes()).decode("ascii")

    return "\n".join(
        [
            # Nightrunner: gunzipped TTF, MUST be format('truetype')
            "@font-face{font-family:'Nightrunner';font-weight:700;"
            f"src:url(data:font/ttf;base64,{b64('Nightrunner-700-normal.ttf')}) format('truetype')}}",
            "@font-face{font-family:'Chakra Petch';font-style:italic;font-weight:700;"
            f"src:url(data:font/woff2;base64,{b64('ChakraPetch-700-italic.woff2')}) format('woff2')}}",
            "@font-face{font-family:'Chakra Petch';font-weight:700;"
            f"src:url(data:font/woff2;base64,{b64('ChakraPetch-700-normal.woff2')}) format('woff2')}}",
            "@font-face{font-family:'Share Tech Mono';font-weight:400;"
            f"src:url(data:font/woff2;base64,{b64('ShareTechMono-400-normal.woff2')}) format('woff2')}}",
        ]
    )


if __name__ == "__main__":
    paths = extract_fonts()
    for name, p in paths.items():
        magic = p.read_bytes()[:4]
        print(f"{name}  {p.stat().st_size} bytes  magic={magic.hex()}")
