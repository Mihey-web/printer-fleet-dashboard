"""Generate the PWA icons in app/web/pwa/ from the favicon's geometry.

Development only: the PNGs are committed, neither the app nor the Pi needs
Pillow. Run with the system Python of the dev PC (it has Pillow):

    py -3 scripts/make_pwa_icons.py

Deterministic: the same constants give byte-identical files.

The favicon (index.html / login.html) in viewBox 32: a rounded square rx=7
#1e293b, the hexagon 16,3 29,12 29,24 16,30 3,24 3,12 stroked #60a5fa 2.5
wide, a dot r=3 in the centre.
- icon-192 / icon-512 (purpose "any"): RGBA, transparent corners, the
  favicon scaled to the size.
- icon-maskable-512 (purpose "maskable"): RGB, the whole square filled; the
  content is shrunk around (16, 16) so its farthest point (vertex 29,24 plus
  half the stroke, ~0.52 of the side) lands inside the 40 % safe circle.
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "web" / "pwa"

VIEW = 32                       # favicon viewBox
SS = 8                          # supersampling: drawn x8, then downscaled
BG = (0x1E, 0x29, 0x3B)
FG = (0x60, 0xA5, 0xFA)
RADIUS = 7
HEXAGON = [(16, 3), (29, 12), (29, 24), (16, 30), (3, 24), (3, 12)]
STROKE = 2.5
DOT_R = 3
CENTER = (16, 16)
MASKABLE_SCALE = 0.72

ICONS = (                       # file, size, maskable
    ("icon-192.png", 192, False),
    ("icon-512.png", 512, False),
    ("icon-maskable-512.png", 512, True),
)


def draw_icon(size: int, maskable: bool) -> Image.Image:
    big = size * SS
    k = big / VIEW
    s = MASKABLE_SCALE if maskable else 1.0
    if maskable:
        img = Image.new("RGB", (big, big), BG)
        fg = FG
    else:
        # Transparent pixels carry the background colour, so the downscale
        # never bleeds black into the rounded edge.
        img = Image.new("RGBA", (big, big), BG + (0,))
        fg = FG + (255,)
    d = ImageDraw.Draw(img)
    if not maskable:
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=round(RADIUS * k), fill=BG + (255,))

    def pt(x, y):
        cx, cy = CENTER
        return ((cx + (x - cx) * s) * k, (cy + (y - cy) * s) * k)

    pts = [pt(x, y) for x, y in HEXAGON]
    # Closed outline: the first segment is repeated so the first vertex is an
    # inner point too and gets the same round joint as the others.
    d.line(pts + pts[:2], fill=fg, width=round(STROKE * s * k), joint="curve")
    cx, cy = pt(*CENTER)
    r = DOT_R * s * k
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fg)
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size, maskable in ICONS:
        path = OUT / name
        draw_icon(size, maskable).save(path, "PNG", optimize=True)
        print("%s %dx%d %s" % (path.relative_to(ROOT), size, size, "RGB" if maskable else "RGBA"))


if __name__ == "__main__":
    main()
