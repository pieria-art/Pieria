"""Render the raster brand assets from the Pieria logo geometry.

`static/logo.svg` is the source of truth for the mark; iOS ignores SVG for the
home-screen icon, so `static/apple-touch-icon.png` has to be a real PNG. There is
no SVG rasterizer in the runtime deps, so we redraw the same geometry with Pillow
and supersample for clean edges.

Keep the constants below in sync with static/logo.svg if the mark ever changes.

    python -m tools.make_icons
"""

from pathlib import Path

from PIL import Image, ImageDraw

# --- geometry, in the logo.svg coordinate space -------------------------------
FRAME = (8, 18, 84, 64)   # x, y, w, h
FRAME_RADIUS = 10
STROKE = 4
RINGS = ((26, 9), (14, 4.8))   # (rx, ry) outer -> inner
RING_CY = 52
DOT_R = 2.6

BG = "#0f172a"        # brand slate-900
FRAME_COLOR = "#f1f5f9"
MARK_COLOR = "#22d3ee"

STATIC = Path(__file__).resolve().parent.parent / "static"


def render(size: int, *, supersample: int = 4, frame_fraction: float = 0.62) -> Image.Image:
    """Draw the mark centred on a square brand-dark tile."""
    n = size * supersample
    img = Image.new("RGB", (n, n), BG)
    d = ImageDraw.Draw(img)

    fx, fy, fw, fh = FRAME
    # scale so the frame (including its stroke) occupies frame_fraction of the tile
    s = (frame_fraction * n) / (fw + STROKE)
    cx = cy = n / 2
    # the rings sit slightly below the frame's centre in the source geometry
    ring_dy = (RING_CY - (fy + fh / 2)) * s

    half_w, half_h = fw * s / 2, fh * s / 2
    d.rounded_rectangle(
        [cx - half_w, cy - half_h, cx + half_w, cy + half_h],
        radius=FRAME_RADIUS * s,
        outline=FRAME_COLOR,
        width=round(STROKE * s),
    )

    for rx, ry in RINGS:
        d.ellipse(
            [cx - rx * s, cy + ring_dy - ry * s, cx + rx * s, cy + ring_dy + ry * s],
            outline=MARK_COLOR,
            width=round(STROKE * s),
        )

    r = DOT_R * s
    d.ellipse([cx - r, cy + ring_dy - r, cx + r, cy + ring_dy + r], fill=MARK_COLOR)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    out = STATIC / "apple-touch-icon.png"
    render(180).save(out, optimize=True)
    print(f"wrote {out} (180x180)")


if __name__ == "__main__":
    main()
