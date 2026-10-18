#!/usr/bin/env python3
"""Render docs/banner.svg, the header at the top of the README.

GitHub strips font-family and style attributes out of a README, so a real font
can only reach that page as a picture. This draws the wordmark with the same
font the dashboard uses and converts every letter to an outline, which means the
banner cannot fall back to something else on a machine that lacks the font. The
file that ships is the SVG; nothing here needs to run again unless the header
changes.

    python3 docs/render-banner.py
    rsvg-convert -w 2800 docs/banner.svg -o /tmp/banner.png   # to look at it
"""
import sys
from pathlib import Path

from fontTools.misc.transform import Transform
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "banner.svg"

# The first of these that exists wins. Both are widely packaged, so whoever
# regenerates this does not have to install the exact same file.
FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Bold.ttf",
    "/usr/share/fonts/TTF/JetBrainsMono-Bold.ttf",
    "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Bold.ttf",
    "/usr/share/fonts/TTF/CaskaydiaCoveNerdFont-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
]

BG_TOP, BG_BOTTOM = "#070b10", "#0a161d"
INK, ACCENT, DIM = "#eaf4f7", "#4fe3ff", "#4a6b78"
W, H = 1400, 340


class Outliner:
    """Text to SVG path data, so the letters do not depend on the reader's fonts."""

    def __init__(self, path: str):
        self.font = TTFont(path)
        self.upem = self.font["head"].unitsPerEm
        self.glyphs = self.font.getGlyphSet()
        self.cmap = self.font.getBestCmap()
        self.advance = self.font["hmtx"]

    def text(self, s: str, size: float, x: float, y: float,
             tracking: float = 0.0) -> tuple[str, float]:
        """Path data for s with its baseline at y, and the width it used."""
        scale = size / self.upem
        parts, pen_x = [], x
        for ch in s:
            name = self.cmap.get(ord(ch))
            if name is None:
                pen_x += self.advance["space"][0] * scale + tracking
                continue
            pen = SVGPathPen(self.glyphs)
            self.glyphs[name].draw(TransformPen(
                pen, Transform(scale, 0, 0, -scale, pen_x, y)))
            parts.append(pen.getCommands())
            pen_x += self.advance[name][0] * scale + tracking
        return " ".join(parts), pen_x - x


def radar(cx: float, cy: float) -> str:
    """The rings from the dashboard, as geometry: no bitmaps, no dependencies."""
    out = []
    for r, opacity in ((44, 0.30), (72, 0.22), (100, 0.16), (126, 0.10)):
        out.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
                   f'stroke="{ACCENT}" stroke-opacity="{opacity}" stroke-width="1.2"/>')
    # the sweep: a wedge from the centre out to a quarter of the outer ring
    out.append(f'<path d="M {cx} {cy} L {cx + 108} {cy - 69} A 127 127 0 0 0 {cx + 84} {cy + 96} Z" '
               f'fill="{ACCENT}" fill-opacity="0.07"/>')
    for i in range(12):
        angle = i * 30
        out.append(f'<line x1="{cx}" y1="{cy - 127}" x2="{cx}" y2="{cy - 121}" '
                   f'stroke="{ACCENT}" stroke-opacity="0.28" stroke-width="1.2" '
                   f'transform="rotate({angle} {cx} {cy})"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="5" fill="{ACCENT}" fill-opacity="0.75"/>')
    return "\n      ".join(out)


def main() -> int:
    font_path = next((p for p in FONT_CANDIDATES if Path(p).exists()), None)
    if font_path is None:
        print("no usable mono font found; looked at:\n  " + "\n  ".join(FONT_CANDIDATES))
        return 1

    outliner = Outliner(font_path)
    wordmark, width = outliner.text("jarvis", 148, 68, 196, tracking=-4)
    tagline, _ = outliner.text("a voice assistant that runs on your own machine", 27, 72, 250)
    caption, _ = outliner.text("local speech in   local speech out   nothing to sign up for",
                               18, 72, 292)
    # The URL is outlined like the rest: a self contained file should not render
    # differently because of whatever monospace font the reader happens to have.
    url, url_width = outliner.text("github.com/Dopethedev/jarvis", 17, 0, H - 26)

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
  <title>jarvis</title>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{BG_TOP}"/>
      <stop offset="1" stop-color="{BG_BOTTOM}"/>
    </linearGradient>
    <linearGradient id="edge" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="{ACCENT}" stop-opacity="0.9"/>
      <stop offset="0.55" stop-color="{ACCENT}" stop-opacity="0.25"/>
      <stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0" stop-color="{ACCENT}" stop-opacity="0.16"/>
      <stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <rect width="{W}" height="{H}" rx="16" fill="url(#bg)"/>
  <rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="16" fill="none"
        stroke="{ACCENT}" stroke-opacity="0.18"/>
  <rect x="24" y="0" width="620" height="3" rx="1.5" fill="url(#edge)"/>

  <circle cx="1150" cy="170" r="200" fill="url(#glow)"/>
  {radar(1150, 170)}

  <path d="{wordmark}" fill="{INK}"/>
  <path d="{tagline}" fill="{ACCENT}" fill-opacity="0.82"/>
  <path d="{caption}" fill="{DIM}"/>

  <g transform="translate({W - 40 - url_width:.1f} 0)"><path d="{url}" fill="{DIM}"/></g>
</svg>
"""
    OUT.write_text(svg)
    print(f"{OUT.relative_to(ROOT)}  {W}x{H}  wordmark {width:.0f}px wide  "
          f"font {Path(font_path).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
