"""
tools/eink_chart.py — sample a photographed ColorChecker Classic (Mini) into `eink_camera`'s row order
(maintainer tool — NOT part of the runtime image).

`eink_camera.solve_camera_matrix` wants (24, 3) linear camera RGB, one row per patch, in the chart's
standard reading order. This module is the piece between a decoded raw frame and that call: it turns
four hand-located corner-patch centres into 24 sampled patch means, and it says which chart patch each
sample IS.

WHY FOUR CORNERS BY HAND, NOT A DETECTOR. The chart occupies ~200 x 300 binned pixels of a 2460 x 1638
frame, on black flock, at a small in-plane rotation that differs between the on-flock frames (ADR-110's
per-frame chart) and the one on-panel frame (X2). A detector would be code that can be wrong silently;
four coordinates read off the JPEG are a fact that can be checked by drawing the sample boxes back onto
the photograph (`draw_boxes`). The corners are then REFINED automatically, each within a small window,
to the position where the sampled box is most uniform — a patch interior is flat, its border is not —
so a 5-10 px reading error costs nothing and a 30 px one shows up as a raised std.

ORIENTATION. The Mini is a PORTRAIT card: 6 rows x 4 columns of patches with the brand text along the
top and the achromatic ladder down the right-hand column, BLACK at the top and WHITE at the bottom.
The reference tables in `eink_camera` are in the Classic's LANDSCAPE reading order (4 rows x 6 columns,
dark skin first, black last). Holding the Mini upright therefore shows the landscape chart rotated 90°
clockwise: landscape row i runs DOWN portrait column i, and landscape column j is portrait row 5-j.
`mini_portrait_to_cc24` is that permutation and `validate_cc24` is the check that it was applied to a
chart in that orientation — the six neutrals must come out as a strictly monotonic ladder, which a
wrong rotation cannot produce.

    python -m tools.eink_chart selftest
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.eink_camera import NEUTRAL_ROWS  # noqa: E402
from tools.eink_measure import _perspective_coeffs  # noqa: E402

MINI_ROWS, MINI_COLS = 6, 4


def grid_centres(corners, rows: int = MINI_ROWS, cols: int = MINI_COLS) -> np.ndarray:
    """(rows, cols, 2) patch centres from the four CORNER-PATCH centres, given as [TL, TR, BR, BL]
    in the photograph's own pixel grid. Projective, not bilinear: the chart is a plane seen off-axis,
    and the same four-point homography the panel rig uses (`_perspective_coeffs`) maps the patch
    lattice onto it exactly."""
    corners = [(float(x), float(y)) for x, y in corners]
    lattice = [(0.0, 0.0), (cols - 1.0, 0.0), (cols - 1.0, rows - 1.0), (0.0, rows - 1.0)]
    a, b, c, d, e, f, g, h = _perspective_coeffs(lattice, corners)
    out = np.empty((rows, cols, 2), dtype=np.float64)
    for r in range(rows):
        for cc in range(cols):
            den = g * cc + h * r + 1.0
            out[r, cc] = ((a * cc + b * r + c) / den, (d * cc + e * r + f) / den)
    return out


def _box_stats(rgb: np.ndarray, cx: float, cy: float, half: int) -> tuple[np.ndarray, float]:
    x0, y0 = int(round(cx)) - half, int(round(cy)) - half
    patch = rgb[y0:y0 + 2 * half, x0:x0 + 2 * half].reshape(-1, 3)
    mean = patch.mean(axis=0)
    # Relative non-uniformity, so a dark patch and a bright one are judged on the same footing.
    return mean, float((patch.std(axis=0) / np.maximum(mean, 1e-9)).mean())


def refine_corners(rgb: np.ndarray, corners, half: int, search: int = 12, step: int = 2) -> list:
    """Nudge each corner-patch centre, within ±`search` px, to where a (2*half)² box is most
    uniform. A patch interior is flat; as soon as the box touches the black border between patches
    the relative std jumps, so the minimum sits inside the patch even from a rough starting point."""
    out = []
    for cx, cy in corners:
        best = None
        for dy in range(-search, search + 1, step):
            for dx in range(-search, search + 1, step):
                _, nu = _box_stats(rgb, cx + dx, cy + dy, half)
                if best is None or nu < best[0]:
                    best = (nu, cx + dx, cy + dy)
        out.append((best[1], best[2]))
    return out


def sample_grid(rgb: np.ndarray, centres: np.ndarray, half: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-patch mean (rows, cols, 3) and relative non-uniformity (rows, cols) over a (2*half)² box."""
    rows, cols = centres.shape[:2]
    means = np.empty((rows, cols, 3), dtype=np.float64)
    nonuni = np.empty((rows, cols), dtype=np.float64)
    for r in range(rows):
        for c in range(cols):
            means[r, c], nonuni[r, c] = _box_stats(rgb, centres[r, c, 0], centres[r, c, 1], half)
    return means, nonuni


def mini_portrait_to_cc24(grid: np.ndarray) -> np.ndarray:
    """(6, 4, ...) portrait-grid values -> (24, ...) in `CC24_NAMES` order.

    Landscape (i, j) — row i of 4, column j of 6 — sits at portrait row 5-j, column i when the Mini
    is held upright (brand text at the top, black patch top-right, white patch bottom-right)."""
    grid = np.asarray(grid)
    if grid.shape[:2] != (MINI_ROWS, MINI_COLS):
        raise ValueError(f"expected a ({MINI_ROWS}, {MINI_COLS}, ...) portrait grid, got {grid.shape}")
    return np.stack([grid[5 - j, i] for i in range(4) for j in range(6)])


def validate_cc24(cc24_rgb: np.ndarray) -> dict:
    """Checks that can FAIL on a wrongly oriented or mis-sampled chart.

    * The achromatic ladder (rows 18-23: white 9.5 -> black 2) must be strictly DECREASING in every
      channel. Any other rotation of the portrait grid puts chromatic patches in those slots.
    * Each neutral's channels must agree to within a per-channel gain — the ratio of each channel to
      the ladder's white must be the same down the ladder to ~10%. A saturated patch in a neutral slot
      fails this; so does a sample box straddling a border.
    """
    rgb = np.asarray(cc24_rgb, dtype=np.float64)
    ladder = rgb[NEUTRAL_ROWS]
    monotonic = bool(np.all(np.diff(ladder, axis=0) < 0))
    ratios = ladder / np.maximum(ladder[0], 1e-12)
    grey = ratios / np.maximum(ratios.mean(axis=1, keepdims=True), 1e-12)
    neutral_spread = float(np.abs(grey - 1.0).max())
    return {"ok": monotonic and neutral_spread < 0.10, "ladder_monotonic": monotonic,
            "neutral_spread": neutral_spread,
            "ladder_white_to_black": float(ladder[0].mean() / max(ladder[-1].mean(), 1e-12))}


def read_chart(rgb: np.ndarray, corners, half: int = 15, refine: bool = True) -> dict:
    """The whole thing: corners -> refined -> lattice -> sampled -> CC24 order -> validated."""
    rgb = np.asarray(rgb, dtype=np.float64)
    if refine:
        corners = refine_corners(rgb, corners, half)
    centres = grid_centres(corners)
    means, nonuni = sample_grid(rgb, centres, half)
    cc24 = mini_portrait_to_cc24(means)
    report = validate_cc24(cc24)
    report.update({"corners": [(float(x), float(y)) for x, y in corners],
                   "centres_cc24": mini_portrait_to_cc24(centres),
                   "nonuniformity_cc24": mini_portrait_to_cc24(nonuni),
                   "worst_nonuniformity": float(nonuni.max())})
    return {"rgb": cc24, "report": report}


def draw_boxes(jpeg_path, centres_cc24: np.ndarray, half: int, out_path, scale: float = 2.0) -> None:
    """Overlay the sample boxes on the camera JPEG so a human can see they sit inside the patches.
    `scale` is the JPEG-to-decoded-frame ratio (2.0 for `eink_raw`'s 2x2 binning)."""
    from PIL import Image, ImageDraw
    im = Image.open(jpeg_path).convert("RGB")
    d = ImageDraw.Draw(im)
    for k, (cx, cy) in enumerate(centres_cc24):
        x, y, hh = cx * scale, cy * scale, half * scale
        d.rectangle([x - hh, y - hh, x + hh, y + hh], outline=(255, 0, 255), width=3)
        d.text((x - hh, y - hh - 14), str(k + 1), fill=(255, 0, 255))
    xs, ys = centres_cc24[:, 0] * scale, centres_cc24[:, 1] * scale
    m = 4 * half * scale
    im.crop((int(xs.min() - m), int(ys.min() - m), int(xs.max() + m), int(ys.max() + m))).save(out_path)


# --- self-test --------------------------------------------------------------------------------------

def _synthetic_chart(seed: int = 0, warp: float = 0.0):
    """A portrait Mini rendered from CC24_NAMES-ordered 'true' RGB, at a rotation/perspective."""
    from PIL import Image, ImageDraw
    rng = np.random.default_rng(seed)
    truth = rng.uniform(0.05, 0.9, (24, 3))
    truth[NEUTRAL_ROWS] = np.linspace(0.9, 0.03, 6)[:, None] * np.array([1.0, 0.95, 1.05])
    grid = np.empty((MINI_ROWS, MINI_COLS, 3))
    for i in range(4):
        for j in range(6):
            grid[5 - j, i] = truth[6 * i + j]
    pitch, patch = 40, 32
    W, H = MINI_COLS * pitch + 40, MINI_ROWS * pitch + 40
    img = Image.new("RGB", (W, H), (5, 5, 5))
    d = ImageDraw.Draw(img)
    for r in range(MINI_ROWS):
        for c in range(MINI_COLS):
            x, y = 20 + c * pitch, 20 + r * pitch
            d.rectangle([x, y, x + patch - 1, y + patch - 1],
                        fill=tuple(int(v * 255) for v in grid[r, c]))
    centre = lambda r, c: (20 + c * pitch + patch / 2, 20 + r * pitch + patch / 2)  # noqa: E731
    corners = [centre(0, 0), centre(0, MINI_COLS - 1), centre(MINI_ROWS - 1, MINI_COLS - 1),
               centre(MINI_ROWS - 1, 0)]
    if warp:
        j = warp * min(W, H)
        src = [(0, 0), (W - 1, 0), (W - 1, H - 1), (0, H - 1)]
        dst = [(rng.uniform(0, j), rng.uniform(0, j)), (W - 1 - rng.uniform(0, j), rng.uniform(0, j)),
               (W - 1 - rng.uniform(0, j), H - 1 - rng.uniform(0, j)),
               (rng.uniform(0, j), H - 1 - rng.uniform(0, j))]
        coeffs = _perspective_coeffs(dst, src)
        img = img.transform((W, H), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
        # Where did the corner centres go? Invert the same mapping numerically via the forward coeffs.
        a, b, c0, d0, e, f, g, h = _perspective_coeffs(src, dst)
        corners = [((a * x + b * y + c0) / (g * x + h * y + 1), (d0 * x + e * y + f) / (g * x + h * y + 1))
                   for x, y in corners]
    return np.asarray(img).astype(np.float64) / 255.0, corners, truth


def cmd_selftest(args) -> None:
    ok = True
    for warp in (0.0, 0.03):
        rgb, corners, truth = _synthetic_chart(seed=3, warp=warp)
        jitter = [(x + 5, y - 4) for x, y in corners]     # a deliberately rough hand reading
        got = read_chart(rgb, jitter, half=6)
        err = np.abs(got["rgb"] - truth).max() * 255
        case = err < 4.0 and got["report"]["ok"]
        ok &= case
        print(f"  warp {warp}: max |error| {err:.2f}/255, ladder ok {got['report']['ladder_monotonic']}, "
              f"spread {got['report']['neutral_spread']:.3f}: {'OK' if case else 'FAILED'}")
    # The orientation check must FAIL on a rotated grid.
    rgb, corners, truth = _synthetic_chart(seed=3)
    rotated = [corners[1], corners[2], corners[3], corners[0]]
    try:
        bad = read_chart(rgb, rotated, half=6)
        case = not bad["report"]["ok"]
    except Exception:     # a 4x6 lattice fed as 6x4 may simply not sample — also a failure, loudly
        case = True
    ok &= case
    print(f"  rotated corners are rejected: {'OK' if case else 'FAILED'}")
    print("\nself-test PASSED" if ok else "\nself-test FAILED")
    if not ok:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    args = ap.parse_args()
    {"selftest": cmd_selftest}[args.cmd](args)


if __name__ == "__main__":
    main()
