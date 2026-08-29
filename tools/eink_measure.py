"""
tools/eink_measure.py — turn a photograph of the panel into measurements
(maintainer tool — NOT part of the runtime image).

The companion to `eink_target.py`. That module renders a frame carrying its own calibration
furniture; this one reads a photograph of it back and recovers what the panel actually produced.

    render -> push to panel -> photograph -> rectify -> normalise -> measure

WHY EACH STEP EXISTS

  * RECTIFY. The black registration frame is the outermost dark structure in the shot by
    construction (a white gutter separates it from the bezel). Its four extreme corners map to
    known render coordinates, which gives a homography — so the photo is resampled back onto the
    render's own pixel grid and can be compared like-for-like. Even on a fixed overhead rig this is
    re-solved per frame: a mount that gets nudged would otherwise corrupt every later measurement
    with no visible symptom.

  * NORMALISE. Room light, viewing angle and the camera's auto white balance distort the capture and
    CHANGE BETWEEN SHOTS, so a camera offset measured once would be wrong by the next photograph.
    The pure-ink patch strip is in every frame, so a per-channel affine correction is solved from
    the same photograph it corrects. Shots taken minutes apart under different light stay comparable.

  * MEASURE. Dithered output must be FUSED (downscaled) before comparison: Floyd-Steinberg carries
    colour in the spatial mix of pure primaries, so per-pixel comparison of a dithered frame is
    meaningless — an early attempt at this measured every pixel as fully saturated and told us
    nothing.

WHAT THIS IS NOT. Absolute colorimetry. A phone or webcam under room light cannot deliver that, and
we do not need it: the job is RANKING recipes against the same reference, and a relative comparison
survives a great deal of camera error once both frames are anchored to the same patches.

SELF-TEST. `selftest` synthesises photographs — known perspective warp, known colour distortion,
noise — and checks the pipeline recovers the known truth. Built that way deliberately, so the code
was validated before any hardware existed rather than debugged against a panel.

    python -m tools.eink_measure selftest
    python -m tools.eink_measure capture --device /dev/video0 --out shot.png
    python -m tools.eink_measure read shot.png --target bench-eink/target_huegrid_1600x1200.png
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import epaper as ep  # noqa: E402
from tools import eink_target as et  # noqa: E402

DARK_MAX = 90          # a pixel this dark, after normalising the frame's own range, is "frame"
DILATE_V_CELLS = 26    # VERTICAL bridging (coarse cells) used only to rejoin split bright regions
DILATE_H_CELLS = 2     # horizontal bridging — deliberately small, see panel_bbox


# --- geometry ------------------------------------------------------------------------------------

def panel_bbox(img: Image.Image, roi=None) -> tuple:
    """Bounding box of the panel's lit area — the LARGEST CONNECTED bright, NEUTRAL region.

    Two refinements, each forced by a real photograph:

      * NEUTRAL, not merely bright. The rig sits on a bright wood floor, and "bright" selected the
        floor along with the panel, so the box spanned the whole frame and every fiducial was then
        searched for in the wrong place. The floor is bright but strongly saturated orange; the
        panel's gutter is bright and near-neutral.
      * CONNECTED, not just the bounding box of matching pixels. A desaturated sage-green door in
        shot also passes bright-and-neutral, and dragged the right edge 300 px past the panel.

    Only needs to be approximately right — it seeds the fiducial search, which then refines.
    """
    if roi:
        x0, y0, x1, y1 = roi
        sub, off = img.crop((x0, y0, x1, y1)), (x0, y0)
    else:
        sub, off = img, (0, 0)
    # Remove the camera's global colour cast BEFORE asking "is this neutral?".
    #
    # ⚠️ The panel is neutral in the WORLD, not in the raw photograph. The C920's locked white balance
    # is a fixed 4000 K that does not match the room, so a grey panel photographs with a cast — and a
    # per-channel gain as ordinary as (0.70, 0.88, 1.35) turns the panel's own white strongly blue.
    # Testing saturation on raw pixels therefore rejects the panel for being the colour the camera
    # made it, and the detector fails with "no neutral area found" while the panel is plainly in shot.
    # Measured: 19 of 42 randomised synthetic captures failed this way before this normalisation.
    #
    # Anchoring on a high per-channel percentile (not the max, which is noise or a specular fleck)
    # makes the mask invariant to any global cast — exactly the invariance wanted here, because we
    # are looking for THE PANEL, not for a colour. It is a detection aid only: the measurement itself
    # is still corrected by the black/white patch affine downstream, untouched by this.
    arr = np.asarray(sub.convert("RGB")).astype(float)
    ref = np.percentile(arr.reshape(-1, 3), 97.0, axis=0)
    balanced = np.clip(arr * (255.0 / np.maximum(ref, 1.0)), 0, 255).astype(np.uint8)
    hsv = np.asarray(Image.fromarray(balanced, "RGB").convert("HSV"))
    sat, val = hsv[..., 1].astype(float), hsv[..., 2].astype(float)
    neutral = sat < 55
    if neutral.sum() < 200:
        raise ValueError("no neutral area found — is the panel in shot?")
    # OTSU, not a fixed cutoff. A fixed "bright" threshold is a statement about the lighting, and it
    # broke the moment the lighting changed: under a raking light the panel's far side fell below
    # V=120, so half of it dropped out of the mask and the panel box came back as its bottom half —
    # which sent every fiducial search to the wrong place. Otsu finds the split between the dark
    # surround and the lit panel from the image's own histogram, so it travels between setups.
    vals = val[neutral]
    hist, _ = np.histogram(vals, bins=64, range=(0, 256))
    tot = hist.sum()
    w0 = np.cumsum(hist)
    w1 = tot - w0
    centres = (np.arange(64) + 0.5) * 4.0
    m0 = np.cumsum(hist * centres) / np.maximum(w0, 1)
    m1 = (np.sum(hist * centres) - np.cumsum(hist * centres)) / np.maximum(w1, 1)
    between = w0 * w1 * (m0 - m1) ** 2
    cut = float(centres[int(np.argmax(between))])
    mask = neutral & (val > cut)
    if mask.sum() < 200:
        raise ValueError("no bright neutral panel area found — check exposure and the ROI")

    # Coarse-grid connected components: cheap, dependency-free, and plenty for a seed box.
    step = 16
    gh, gw = mask.shape[0] // step, mask.shape[1] // step
    if gh < 3 or gw < 3:
        ys, xs = np.nonzero(mask)
        return (int(xs.min()) + off[0], int(ys.min()) + off[1],
                int(xs.max()) + off[0], int(ys.max()) + off[1])
    coarse = mask[:gh * step, :gw * step].reshape(gh, step, gw, step).mean(axis=(1, 3)) > 0.5

    # DILATE before labelling, ANISOTROPICALLY. The target's content bands span the full width with
    # dark cells and split the white area into a top and a bottom component — and the bottom one is
    # larger, so the panel box came back as its lower half.
    #
    # ⚠️ What actually holds the two halves together is the target's 10 px outer white gutter, which
    # fills 0.625 of a 16 px cell — barely over the 0.5 coverage threshold. Any warp, blur or
    # resampling pushes it under, and the panel splits. Measured on self-test case 4 (4% warp): six
    # grid rows dropped to ZERO cells and the seed box came back 1504x752 instead of 1600x1200.
    # Relying on a 10 px feature for connectivity is the underlying fragility; bridging removes it.
    #
    # The bridging is vertical-dominant on purpose. The bands that split our targets run HORIZONTALLY
    # (content rows, the patch strip), so vertical bridging is what rejoins the panel to itself. The
    # intruders that forced connected-components in the first place — the wood floor, the sage door —
    # were LATERAL, so horizontal bridging is kept small and cannot reach them. Over-dilating
    # vertically is cheap because the box below is taken from the UNDILATED cells.
    undilated = coarse.copy()
    grown = coarse.copy()
    for _ in range(DILATE_V_CELLS):
        g = grown.copy()
        g[1:, :] |= grown[:-1, :]
        g[:-1, :] |= grown[1:, :]
        grown = g
    for _ in range(DILATE_H_CELLS):
        g = grown.copy()
        g[:, 1:] |= grown[:, :-1]
        g[:, :-1] |= grown[:, 1:]
        grown = g
    coarse = grown

    seen = np.zeros_like(coarse, dtype=bool)
    best = None
    for sy in range(gh):
        for sx in range(gw):
            if not coarse[sy, sx] or seen[sy, sx]:
                continue
            stack, cells = [(sy, sx)], []
            seen[sy, sx] = True
            while stack:
                cy, cx = stack.pop()
                cells.append((cy, cx))
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < gh and 0 <= nx < gw and coarse[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if best is None or len(cells) > len(best):
                best = cells
    # Undo the dilation by taking the box over the ORIGINAL cells of the winning component, not by
    # shrinking the grown box. The dilation exists to decide CONNECTIVITY; it must not contribute to
    # the ANSWER. The previous version subtracted a flat DILATE_CELLS*step from every side, which is
    # only correct when the mask actually over-grew by that much. On a clean capture — dark surround,
    # panel on black fabric, nothing bright and neutral nearby, which is the rig as it stands now —
    # nothing over-grows, so the subtraction pulled the seed box ~48 px INSIDE the panel, displaced
    # every expected fiducial position, and made the search miss under perspective. That is a silent
    # registration error, not a visible failure: it broke self-test case 4 (4% warp) in a20d785 and
    # went unnoticed through the whole 2026-08-28 measurement session.
    component = np.zeros_like(coarse, dtype=bool)
    for cy, cx in best:
        component[cy, cx] = True
    keep = component & undilated
    if not keep.any():                    # component exists only in the dilated mask — keep the grown box
        keep = component
    ys, xs = np.nonzero(keep)
    x0, y0 = int(xs.min()) * step, int(ys.min()) * step
    x1, y1 = (int(xs.max()) + 1) * step, (int(ys.max()) + 1) * step
    return (x0 + off[0], y0 + off[1], x1 + off[0], y1 + off[1])


def find_fiducials(img: Image.Image, w: int, h: int, roi=None) -> list:
    """Centres of the four corner fiducials in photo pixels: tl, tr, br, bl.

    ⚠️ THIS REPLACED A DETECTOR THAT LOOKED FOR THE OUTERMOST DARK RECTANGLE. On synthetic photos
    that worked; on the real panel it locked onto the BEZEL, which is dark and sits immediately
    outside the registration frame. The rectified image came back containing the Pimoroni silkscreen
    and the flex cable, patch rectangles straddled boundaries, and the numbers looked like a
    mis-calibrated camera rather than a registration failure.

    The fix was to the TARGET rather than to the detector: fiducials are drawn well inboard, where
    nothing else is dark, so there is nothing to confuse them with. Each is then found as the dark
    centroid inside a window around where it is expected — which needs only a rough panel box to
    start from, and tolerates rotation and perspective comfortably.
    """
    px0, py0, px1, py1 = panel_bbox(img, roi)
    pw, ph = px1 - px0, py1 - py0
    if pw < 50 or ph < 50:
        raise ValueError(f"panel box too small to work with: {pw}x{ph}")
    a = np.asarray(img.convert("L")).astype(float)
    lo, hi = float(a[py0:py1, px0:px1].min()), float(a[py0:py1, px0:px1].max())
    norm = (a - lo) / max(hi - lo, 1e-6) * 255.0

    out = []
    for fx, fy in et.fiducial_centres(w, h):
        # expected position as a fraction of the render, mapped onto the rough panel box
        ex = px0 + pw * (fx / w)
        ey = py0 + ph * (fy / h)
        # Window must not be able to reach the content: FID_CLEAR is the clearance the target
        # guarantees, so stay inside it.
        scale = min(pw / w, ph / h)
        win = max(14, int(scale * (et.FID_SIZE / 2 + et.FID_CLEAR * 0.8)))
        wx0, wx1 = int(max(px0, ex - win)), int(min(px1, ex + win))
        wy0, wy1 = int(max(py0, ey - win)), int(min(py1, ey + win))
        cell = norm[wy0:wy1, wx0:wx1]
        # LOCAL threshold, not a global one. A fiducial sitting in a vignetted corner is far brighter
        # in absolute terms than one under the light, so a single global cutoff finds some fiducials
        # and misses others — which showed up as the two right-hand fiducials disagreeing by 69 px
        # while the two top ones agreed to 7. Thresholding against the window's own range makes
        # detection independent of how that corner happens to be lit.
        c_lo, c_hi = float(cell.min()), float(cell.max())
        local_cut = c_lo + 0.45 * max(c_hi - c_lo, 1.0)
        ys, xs = np.nonzero(cell <= min(local_cut, DARK_MAX * 1.8))
        if len(xs) < 12:
            raise ValueError(
                f"fiducial near ({int(ex)},{int(ey)}) not found — is the whole panel in shot, and "
                f"is the target one that carries fiducials? Re-render targets after updating "
                f"eink_target.py.")
        # Refine iteratively. A plain centroid of every dark pixel in the window is dragged by any
        # other dark thing that creeps into it — measured 55 px off when a black content cell sat
        # near a fiducial. Re-centroiding within a fiducial-sized radius converges onto the fiducial
        # and drops the intruder, because the intruder is farther away by construction.
        cx, cy = xs.mean(), ys.mean()
        rad = max(8.0, scale * et.FID_SIZE * 0.6)
        for _ in range(3):
            keep = ((xs - cx) ** 2 + (ys - cy) ** 2) <= rad ** 2
            if keep.sum() < 12:
                break
            cx, cy = xs[keep].mean(), ys[keep].mean()
        out.append((float(cx + wx0), float(cy + wy0)))
    return out


def _perspective_coeffs(dst_corners, src_corners) -> tuple:
    """Solve the 8 coefficients PIL needs: it maps DEST pixels back into the SOURCE image."""
    A, b = [], []
    for (dx, dy), (sx, sy) in zip(dst_corners, src_corners):
        A.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy]); b.append(sx)
        A.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy]); b.append(sy)
    res = np.linalg.solve(np.asarray(A, dtype=float), np.asarray(b, dtype=float))
    return tuple(res.tolist())


def refine_fiducials(photo: Image.Image, w: int, h: int, src: list, iters: int = 2) -> list:
    """Re-find each fiducial in a TIGHT window predicted by the current homography.

    ⚠️ THE FIRST PASS IS SEEDED BY panel_bbox, AND THAT SEED BIASES THE SCALE. find_fiducials places
    its search windows using a rough bright-region box; when that box is off, every window is off the
    same way, each centroid is pulled toward its window centre, and the result is not a random error
    but a systematic one — the four points move together, so the solved homography comes out at the
    wrong SCALE while still fitting its own four points perfectly.

    Measured 2026-08-29 by cross-correlating the rectified photograph against the digital target:
    best match at scale 0.96 with a 16/32 px translation, i.e. a 4% scale error, which is ~48 px
    across the content — half a cell on a dense grid. Nothing upstream reports it, because a
    homography always fits its own four points.

    Iterating removes the dependence on the seed: predict where each fiducial should be from the
    current solution, search a window tight enough that only the fiducial can be inside it, re-solve.
    Two passes are enough; the correction is monotone and small after the first.
    """
    dst = [(float(x), float(y)) for x, y in et.fiducial_centres(w, h)]
    a = np.asarray(photo.convert("L")).astype(float)
    cur = [(float(x), float(y)) for x, y in src]
    for _ in range(iters):
        try:
            coeffs = _perspective_coeffs(dst, cur)      # render -> photo
        except Exception:
            return cur
        out = []
        for (ex, ey) in dst:
            den = coeffs[6] * ex + coeffs[7] * ey + 1.0
            px = (coeffs[0] * ex + coeffs[1] * ey + coeffs[2]) / den
            py = (coeffs[3] * ex + coeffs[4] * ey + coeffs[5]) / den
            win = int(et.FID_SIZE * 0.9)
            x0, x1 = int(px - win), int(px + win)
            y0, y1 = int(py - win), int(py + win)
            if x0 < 0 or y0 < 0 or x1 > a.shape[1] or y1 > a.shape[0]:
                out.append((px, py))
                continue
            cell = a[y0:y1, x0:x1]
            lo, hi = float(cell.min()), float(cell.max())
            m = cell <= lo + 0.45 * max(hi - lo, 1.0)
            ys, xs = np.nonzero(m)
            out.append((x0 + xs.mean(), y0 + ys.mean()) if len(xs) >= 12 else (px, py))
        cur = out
    return cur


def rectify(photo: Image.Image, w: int, h: int, roi=None) -> Image.Image:
    """Resample the photo onto the render's pixel grid using the corner fiducials."""
    src = refine_fiducials(photo, w, h, find_fiducials(photo, w, h, roi))
    dst = [(float(x), float(y)) for x, y in et.fiducial_centres(w, h)]
    coeffs = _perspective_coeffs(dst, src)
    return photo.convert("RGB").transform((w, h), Image.PERSPECTIVE, coeffs, Image.BICUBIC)


# --- photometry ----------------------------------------------------------------------------------

def strip_dy(rectified: Image.Image, w: int, h: int, search: int = 48) -> int:
    """Vertical correction for the calibration strip, found from the strip's own six patches.

    ⚠️ WHY THIS IS NEEDED EVEN THOUGH THE HOMOGRAPHY IS EXACT. A four-point homography fits its four
    points BY CONSTRUCTION, so lens distortion never shows up AT the fiducials — it shows up BETWEEN
    them, and the calibration strip sits between them. Measured 2026-08-29 on the real panel: all four
    fiducials mapped with error 0.0, while the black calibration patch actually occupied rows 876-965
    against a nominal 900-996.

    That 25 px is harmless for the large ink FIELDS, which are 345 px tall and sampled with a 0.30
    inset, i.e. 103 px of margin. It is fatal for the strip, which is 96 px tall with a 0.22 inset —
    21 px of margin — so the sampling window walks off the patch and onto the white canvas below it.
    The visible symptoms were an inflated black anchor and, downstream of the affine it anchors, every
    ink darker than it crushed to zero. It reads exactly like a scatter or lighting problem.

    Found JOINTLY across all six patches rather than per patch: the offset that minimises the total
    within-patch variation is the one that centres the strip. Solving all six together is what keeps
    it honest — a per-patch search lets the black patch slide onto the black registration frame, which
    scores beautifully and is completely wrong.
    """
    a = np.asarray(rectified.convert("L")).astype(float)
    base = patch_rects(w, h, len(et.STRIP_ORDER), dy=0)
    best, best_dy = None, 0
    for dy in range(-search, search + 1, 2):
        tot = 0.0
        ok = True
        for (x0, y0, x1, y1) in base:
            sy0, sy1 = y0 + dy + int((y1 - y0) * 0.22), y1 + dy - int((y1 - y0) * 0.22)
            sx0, sx1 = x0 + int((x1 - x0) * 0.22), x1 - int((x1 - x0) * 0.22)
            if sy0 < 0 or sy1 > a.shape[0] or sy1 <= sy0:
                ok = False
                break
            tot += float(a[sy0:sy1, sx0:sx1].std())
        if ok and (best is None or tot < best):
            best, best_dy = tot, dy
    return best_dy


def patch_rects(w: int, h: int, count: int, dy: int = 0) -> list:
    x0, y0, x1, y1 = et.content_box(w, h)
    sy0 = y1 + et.PATCH_GAP + dy
    total = x1 - x0
    pw = (total - et.PATCH_GAP * (count - 1)) / count
    out = []
    for i in range(count):
        px0 = int(round(x0 + i * (pw + et.PATCH_GAP)))
        px1 = int(round(px0 + pw))
        out.append((px0, sy0, px1, sy0 + et.PATCH_H))
    return out


def _mean_rgb(img: Image.Image, rect, inset: float = 0.22) -> np.ndarray:
    """Mean of a patch's INTERIOR. The inset avoids edge bleed from resampling and from the
    specular highlight that room light puts somewhere on the glass."""
    x0, y0, x1, y1 = rect
    dx, dy = int((x1 - x0) * inset), int((y1 - y0) * inset)
    a = np.asarray(img.crop((x0 + dx, y0 + dy, x1 - dx, y1 - dy)).convert("RGB")).astype(float)
    return a.reshape(-1, 3).mean(axis=0)


def solve_correction(rectified: Image.Image, w: int, h: int) -> tuple:
    """Per-channel affine solved from the BLACK and WHITE patches ONLY.

    ⚠️ A first version anchored on all six inks, mapping them to SPECTRA6_OUTPUT_PALETTE — the PURE
    primaries. That is wrong twice over. The panel physically cannot emit pure (255,0,0); it emits a
    muted ink. So the fit was asked to reach impossible targets and responded with extreme gains and
    clipping (measured: residual 65-108/255, a negative channel gain). And it was CIRCULAR: the
    chromatic inks' true values are exactly what we are trying to measure, so they cannot also be the
    anchors.

    Black and white are the only legitimate anchors, because their role is definitional rather than
    measured: they set the tonal range, absorbing exposure and white balance. Everything is then
    expressed in PANEL-RELATIVE units — the panel's own black is 0 and its own white is 255 — which
    is self-consistent, needs no prior knowledge of the inks, and is directly comparable to any other
    palette normalised the same way.
    """
    rects = patch_rects(w, h, len(ep.SPECTRA6_OUTPUT_PALETTE), dy=strip_dy(rectified, w, h))
    # Index by NAME, not by position: the strip is laid out in et.STRIP_ORDER, which puts the two
    # anchors at opposite ends so the black one is not lifted by scatter from the white one.
    black = _mean_rgb(rectified, rects[et.STRIP_ORDER.index("black")], inset=0.30)
    white = _mean_rgb(rectified, rects[et.STRIP_ORDER.index("white")], inset=0.30)
    span = np.maximum(white - black, 1e-3)
    gain = 255.0 / span
    off = -black * gain
    # Diagnostic: how uniform is each patch? A specular highlight or uneven light shows up here long
    # before it shows up as a wrong colour, and silently biases every later measurement.
    stds = []
    for r in rects:
        x0, y0, x1, y1 = r
        dx, dy = int((x1 - x0) * 0.22), int((y1 - y0) * 0.22)
        a = np.asarray(rectified.crop((x0 + dx, y0 + dy, x1 - dx, y1 - dy))).astype(float)
        stds.append(float(a.reshape(-1, 3).std(axis=0).mean()))
    return gain, off, float(np.mean(stds))


def normalise_palette(palette) -> list:
    """Express an assumed palette in the same panel-relative units, so it can be compared."""
    arr = np.asarray([list(c) for c in palette], dtype=float)
    black, white = arr[0], arr[1]
    span = np.maximum(white - black, 1e-3)
    return [[round(v, 1) for v in ((c - black) * 255.0 / span)] for c in arr]


def apply_correction(img: Image.Image, gain, off) -> Image.Image:
    a = np.asarray(img.convert("RGB")).astype(float) * gain + off
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


# --- measurement ---------------------------------------------------------------------------------

def fuse(img: Image.Image, factor: int = 8) -> np.ndarray:
    return np.asarray(img.resize((max(1, img.width // factor), max(1, img.height // factor)),
                                 Image.BOX)).astype(float)


def build_flat_field(flat_photo: Image.Image, w: int, h: int, roi=None, smooth: int = 40):
    """Smooth illumination map from a photograph of an all-white panel, in render coordinates.

    Heavily smoothed on purpose: illumination genuinely varies slowly across a panel, and the blur
    also averages away the registration frame and the fiducials, which are dark and would otherwise
    punch holes in the map.
    """
    rect = rectify(flat_photo, w, h, roi)
    import tools.eink_target as _et
    cx0, cy0, cx1, cy1 = _et.content_box(w, h)
    content_mean = float(np.asarray(rect.convert("L")).astype(float)[cy0:cy1, cx0:cx1].mean())
    if content_mean < 90:
        raise ValueError(
            f"flat-field content mean is {content_mean:.0f} — far too dark for an all-white target. "
            f"This is almost certainly a mid-refresh capture (a Spectra 6 inversion phase photographs "
            f"as a dark wash). Re-render `target flat`, wait for the panel, and re-capture.")
    small = rect.resize((max(4, w // smooth), max(4, h // smooth)), Image.BOX)
    field = np.asarray(small.resize((w, h), Image.BICUBIC)).astype(float)
    return np.maximum(field, 1.0)


def align_to_reference(rect: Image.Image, reference: Image.Image, w: int, h: int,
                       max_shift: int = 90, prior=None) -> Image.Image:
    """Snap a rectified photograph onto the render grid using the RENDER ITSELF as the reference.

    ⚠️ WHY THE HOMOGRAPHY IS NOT THE END OF THE STORY. It is solved from four fiducials, so it fits
    those four points exactly and reports no error no matter how wrong it is elsewhere. Two things
    then survive it: lens distortion, which is zero at the fitted points and grows between them, and
    a systematic bias in the fiducial centroids themselves, which moves all four together and comes
    out as a SCALE error. Measured 2026-08-29 by cross-correlating against the digital target: best
    match at scale 0.96 with a 16/32 px translation — about 48 px across the content, half a cell on
    a dense grid, and completely invisible to every check upstream.

    We always know exactly what was sent to the panel, so the render is the perfect reference. A
    coarse scale-and-translation search against it measures the residual directly instead of
    inferring it, and it is the only check here that can fail loudly: a low correlation means the
    photograph does not show the target we think it shows.
    """
    # ⚠️ ALIGNMENT IS A PROPERTY OF THE RIG, NOT OF THE TARGET. Camera and panel do not move between
    # rows, so the residual is the same for every capture in a session — and searching for it per row
    # is strictly worse than measuring it once on a well-structured target and reusing it. Measured
    # 2026-08-29: the structured targets (inkmix, huevalue, surround, edges) all agreed on
    # scale 0.94, dx~6, dy-42 with patch residuals of 2.0-2.3, while the low-contrast ones disagreed
    # wildly and scored 9-13 — `tonefine`, a ladder of grey steps, pinned dx at the +90 search LIMIT,
    # which is a boundary result and therefore a request to widen rather than an answer.
    #
    # A neutral tone ramp simply does not carry enough horizontal structure to localise, so its
    # correlation surface is nearly flat and the argmax is noise. Passing the rig's alignment in as
    # `prior` is what makes those targets measurable at all.
    if prior is not None:
        corr, scale, dx, dy = prior
        inv = 1.0 / scale
        nw_, nh_ = int(w * inv), int(h * inv)
        moved = Image.new("RGB", (w, h), (255, 255, 255))
        moved.paste(rect.convert("RGB").resize((nw_, nh_), Image.BICUBIC),
                    (int((w - nw_) / 2 - dx * inv), int((h - nh_) / 2 - dy * inv)))
        moved.info["align"] = (corr, scale, dx, dy)
        return moved

    # ⚠️ CORRELATE ON THE CONTENT, NOT ON THE WHOLE FRAME. The registration furniture — frame,
    # fiducials, patch strip — is identical in every target and is ALREADY aligned by the homography,
    # so including it lets it dominate the correlation and pin a spurious peak. That is fatal for a
    # low-contrast target: `tonefine` is mostly white paper, its furniture outweighed its 13 grey
    # bars, and the "alignment" it produced was worse than doing nothing (patch residual 12.0 against
    # 2.3, and a tone ramp that ran backwards). Masking to the content box makes the search measure
    # the thing we actually need aligned.
    cx0, cy0, cx1, cy1 = et.content_box(w, h)
    # Search at 1/4 scale: we are looking for offsets of tens of pixels, so full resolution buys
    # nothing and costs 16x. The result is scaled back up before use.
    DS = 4
    a_full = np.asarray(rect.convert("L")).astype(float)[cy0:cy1, cx0:cx1]
    ref_full = np.asarray(reference.convert("L").resize((w, h), Image.BILINEAR)).astype(float)
    t_full = ref_full[cy0:cy1, cx0:cx1]
    a = np.asarray(Image.fromarray(a_full.astype(np.uint8)).resize(
        (a_full.shape[1] // DS, a_full.shape[0] // DS), Image.BOX)).astype(float)
    t0 = np.asarray(Image.fromarray(t_full.astype(np.uint8)).resize(
        (t_full.shape[1] // DS, t_full.shape[0] // DS), Image.BOX)).astype(float)
    ch_, cw_ = a.shape

    def n(x):
        return (x - x.mean()) / (x.std() + 1e-9)

    best = None
    for scale in (0.96, 0.98, 1.00, 1.02, 1.04):
        tw, th = int(cw_ * scale), int(ch_ * scale)
        t = np.asarray(Image.fromarray(t0.astype(np.uint8)).resize((tw, th), Image.BILINEAR)
                       ).astype(float)
        for dy in range(-max_shift // DS, max_shift // DS + 1):
            for dx in range(-max_shift // DS, max_shift // DS + 1):
                oy, ox = (ch_ - th) // 2 + dy, (cw_ - tw) // 2 + dx
                ys0, ys1 = max(0, oy), min(ch_, oy + th)
                xs0, xs1 = max(0, ox), min(cw_, ox + tw)
                if ys1 - ys0 < ch_ * 0.6 or xs1 - xs0 < cw_ * 0.6:
                    continue
                c = float((n(a[ys0:ys1, xs0:xs1]) * n(t[ys0 - oy:ys1 - oy, xs0 - ox:xs1 - ox])).mean())
                if best is None or c > best[0]:
                    best = (c, scale, dx, dy)
    corr, scale, dx, dy = best
    dx, dy = dx * DS, dy * DS          # back to full-resolution pixels
    # Invert the found (scale, translation) so the PHOTO lands on the render grid.
    inv = 1.0 / scale
    nw_, nh_ = int(w * inv), int(h * inv)
    moved = Image.new("RGB", (w, h), (255, 255, 255))
    src = rect.convert("RGB").resize((nw_, nh_), Image.BICUBIC)
    moved.paste(src, (int((w - nw_) / 2 - dx * inv), int((h - nh_) / 2 - dy * inv)))
    moved.info["align"] = (round(corr, 3), scale, dx, dy)
    return moved


def read_panel(photo: Image.Image, w: int, h: int, roi=None, flat=None, reference=None,
               align_prior=None) -> dict:
    rect = rectify(photo, w, h, roi)
    if flat is not None:
        a = np.asarray(rect).astype(float)
        a = a / flat * float(flat.mean())
        rect = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")
    # ⚠️ ORDER MATTERS, AND GETTING IT WRONG IS CATASTROPHIC AND QUIET.
    #
    # The homography puts the calibration FURNITURE — frame, fiducials, patch strip — exactly at its
    # nominal positions; that is what it is solved to do. solve_correction reads the black and white
    # anchors from the strip at those nominal positions.
    #
    # align_to_reference then shifts the WHOLE image, furniture included, to line the CONTENT up.
    # Running it before solve_correction therefore moves the anchors out from under the very function
    # that depends on them: the affine is solved from whatever now sits at the nominal strip
    # coordinates, the gain comes out absurd, and the corrected image blows out to near-binary noise.
    #
    # It is quiet because patch_residual cannot see it — that metric measures within-patch
    # UNIFORMITY, not whether the gain is sane, so it kept reporting a healthy 2-3 while the image
    # was destroyed. The visible symptom was a tone ramp reading as all zeros.
    #
    # So: correct the tones FIRST, while the furniture is still where the homography put it, and
    # align only afterwards, for the content readout.
    gain, off, resid = solve_correction(rect, w, h)
    corrected = apply_correction(rect, gain, off)
    align = None
    if reference is not None:
        corrected = align_to_reference(corrected, reference, w, h, prior=align_prior)
        align = corrected.info.get("align")
    return {"rectified": rect, "corrected": corrected, "align": align,
            "gain": gain.tolist(), "offset": off.tolist(), "patch_residual": resid}


def measured_primaries(corrected: Image.Image, w: int, h: int) -> dict:
    """What the panel ACTUALLY produced for each pure ink, in the corrected photo's terms.

    The point of the `primaries` target: `epaper.SPECTRA6_DITHER_PALETTE` is Pimoroni's measurement
    of a different EL133UF1, and every distance calculation in the renderer assumes it.
    """
    x0, y0, x1, y1 = et.content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    out = {}
    for i, name in enumerate(et.INK_NAMES):
        cx, cy = i % 3, i // 3
        rx0 = x0 + cx * cw // 3
        ry0 = y0 + cy * ch // 2
        rect = (rx0, ry0, x0 + (cx + 1) * cw // 3, y0 + (cy + 1) * ch // 2)
        out[name] = [round(v, 1) for v in _mean_rgb(corrected, rect, inset=0.30)]
    return out


# --- self-test -----------------------------------------------------------------------------------

def _synthesise_photo(target: Image.Image, warp: float, gain, off, noise: float,
                      seed: int) -> Image.Image:
    """Fake a photograph: perspective, camera colour distortion, sensor noise, dark surround."""
    rng = np.random.default_rng(seed)
    w, h = target.size
    pad = int(max(w, h) * 0.12)
    canvas = Image.new("RGB", (w + 2 * pad, h + 2 * pad), (18, 18, 20))   # dark bezel/surround
    canvas.paste(target, (pad, pad))
    W, H = canvas.size
    j = warp * min(W, H)
    src = [(0, 0), (W - 1, 0), (W - 1, H - 1), (0, H - 1)]
    dst = [(rng.uniform(0, j), rng.uniform(0, j)),
           (W - 1 - rng.uniform(0, j), rng.uniform(0, j)),
           (W - 1 - rng.uniform(0, j), H - 1 - rng.uniform(0, j)),
           (rng.uniform(0, j), H - 1 - rng.uniform(0, j))]
    warped = canvas.transform((W, H), Image.PERSPECTIVE, _perspective_coeffs(dst, src), Image.BICUBIC)
    a = np.asarray(warped).astype(float)
    a = a * np.asarray(gain) + np.asarray(off)
    a += rng.normal(0, noise, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


def cmd_selftest(args) -> None:
    w, h = 1600, 1200
    target = et.compose(et.target_primaries(w, h), w, h)
    print("SELF-TEST — synthesise photographs with KNOWN distortion, check recovery\n")
    ok = True
    for i, (warp, gain, off, noise) in enumerate([
            (0.000, (1.00, 1.00, 1.00), (0, 0, 0), 0.0),
            (0.010, (0.92, 1.00, 1.12), (10, 4, -6), 2.0),
            (0.025, (0.80, 0.95, 1.25), (24, 8, -14), 4.0),
            (0.040, (0.70, 0.88, 1.35), (30, 12, -20), 6.0)], 1):
        photo = _synthesise_photo(target, warp, gain, off, noise, seed=i)
        pad = int(max(w, h) * 0.12)
        roi = (pad // 2, pad // 2, photo.width - pad // 2, photo.height - pad // 2)
        try:
            r = read_panel(photo, w, h, roi=roi)
        except Exception as exc:
            print(f"  case {i}: FAILED to read — {exc}"); ok = False; continue
        prim = measured_primaries(r["corrected"], w, h)
        truth = {n: tuple(c) for n, c in zip(et.INK_NAMES, ep.SPECTRA6_OUTPUT_PALETTE)}
        err = max(max(abs(a - b) for a, b in zip(prim[n], truth[n])) for n in truth)
        verdict = "OK" if err <= 12 else "TOO HIGH"
        if err > 12:
            ok = False
        print(f"  case {i}: warp {warp:.3f}  camera gain {gain}  noise {noise:.0f}")
        print(f"           patch residual {r['patch_residual']:5.1f}   worst ink error {err:5.1f}  {verdict}")
    print("\nself-test PASSED" if ok else "\nself-test FAILED")
    if not ok:
        sys.exit(1)


def _grab(device: str, size: str, warmup: int, out: str) -> None:
    # ⚠️ THE CONTROLS MUST BE WRITTEN WHILE THE STREAM IS OPEN, not before it.
    #
    # Writes made before ffmpeg opens the device are discarded at stream start: measured 2026-08-29,
    # exposure read back as 400 immediately after being written, then 156 after a single grab, and
    # gain walked from a locked 24 to 205 on its own. The give-away was an exposure sweep whose frame
    # means cycled with a period of THREE regardless of the value requested — each reading was the
    # previous grab's drifted state. Re-asserting immediately before the grab does NOT help, because
    # the reset happens when the stream opens, which is after that write.
    #
    # So a helper thread re-asserts them continuously for the life of the capture. With it, the
    # controls read back correct afterwards and the frame responds to exposure monotonically; without
    # it, neither is true. This supersedes the earlier gain-only re-assert, which fixed half of the
    # problem and hid the other half.
    stop = threading.Event()

    def _hold() -> None:
        while not stop.is_set():
            for ctrl, val in PREGRAB_CTRLS:
                _v4l2_set(device, ctrl, val)
            stop.wait(0.08)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    try:
        _grab_stream(device, size, warmup, out)
    finally:
        stop.set()
        holder.join(timeout=2.0)


def _grab_stream(device: str, size: str, warmup: int, out: str) -> None:
    # MJPEG explicitly: at 1080p the C920 offers MJPG at 30 fps but raw YUYV only at ~5 fps, and
    # ffmpeg will happily pick the slow one — which turns a one-frame grab into a long stall and,
    # on a marginal USB port, into a wedged device.
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2",
           "-input_format", "mjpeg", "-video_size", size, "-i", device]
    if warmup > 0:
        cmd += ["-vf", f"select=gte(n\\,{warmup})"]
    cmd += ["-frames:v", "1", "-y", out]
    subprocess.run(cmd, check=True)


def _frame_delta(a: Path, b: Path) -> float:
    """Mean absolute difference between two frames, 0-255."""
    x = np.asarray(Image.open(a).convert("L").resize((320, 180), Image.BOX)).astype(float)
    y = np.asarray(Image.open(b).convert("L").resize((320, 180), Image.BOX)).astype(float)
    return float(np.abs(x - y).mean())


#: Locked C920 settings for the overhead rig, found by sweeping against the panel 2026-08-28.
#: ⚠️ EXPOSURE IS LIGHTING-SPECIFIC AND MUST BE RE-SWEPT WHENEVER THE LIGHT CHANGES. 620 suits
#: ambient-only (curtain closed, no lamp): panel max 214, ZERO clipped pixels on the panel. Under a
#: clip light the same rig wanted 200. Getting this wrong is not subtle — at the ambient level, an
#: exposure tuned for lamplight left the frame too dark to even locate the panel.
#: A clipped white anchor invalidates the whole correction, so headroom beats brightness. The C920
#: quantises exposure coarsely: 560-740 are indistinguishable, as were 150-200 under the lamp.
CAMERA_LOCK = [
    ("auto_exposure", 1),                 # 1 = Manual Mode
    ("exposure_time_absolute", 2047),
    ("white_balance_automatic", 0),
    ("white_balance_temperature", 4000),
    ("exposure_dynamic_framerate", 0),
    ("backlight_compensation", 0),
    ("power_line_frequency", 2),          # 60 Hz — stops mains flicker beating with the shutter
    ("gain", 72),
]
#: ⚠️ The C920 drives GAIN back up on its own (measured: 0 -> 109 -> 255 as exposure rose) even in
#: manual exposure mode. Every capture therefore re-asserts it immediately before grabbing. Without
#: this the panel clips ~30% of its pixels and every measurement built on it is silently wrong.
GAIN_CTRL = ("gain", 72)

#: Controls re-asserted immediately before EVERY grab, because this camera silently walks them back.
#:
#: ⚠️ Gain was known to drift (0 -> 109 -> 255 measured during setup) and was already re-asserted
#: here. EXPOSURE DRIFTS THE SAME WAY and was not, which was found on 2026-08-29: with the control
#: read back as 400 immediately after writing it, a single grab left it at 156, while gain had walked
#: from the locked 24 to 205 on its own. The symptom is nasty because it is quiet — an exposure sweep
#: returned frame means that cycled with a period of 3 regardless of the value set, because each
#: reading reflected the PREVIOUS grab's drifted state rather than the requested one.
#:
#: The per-photograph black/white affine absorbs a global exposure change, so colour ratios survive
#: this. What does not survive is the clipping budget: an exposure that wanders upward clips the
#: white anchor, and a clipped anchor invalidates the whole correction. Re-assert, then verify.
PREGRAB_CTRLS = [(c, v) for c, v in CAMERA_LOCK
                 if c in ("auto_exposure", "exposure_time_absolute", "gain")]


def _v4l2_set(device: str, ctrl: str, value) -> None:
    subprocess.run(["v4l2-ctl", "-d", device, f"--set-ctrl={ctrl}={value}"],
                   check=False, capture_output=True)


def _v4l2_get(device: str, ctrl: str):
    r = subprocess.run(["v4l2-ctl", "-d", device, f"--get-ctrl={ctrl}"],
                       check=False, capture_output=True, text=True)
    try:
        # menu controls read back as "auto_exposure: 1 (Manual Mode)" — take the leading integer
        return int(r.stdout.strip().split(":")[1].strip().split()[0])
    except (IndexError, ValueError):
        return None


def score_against_reference(photo: Image.Image, ref: Image.Image, w: int, h: int,
                            flat=None, roi=None, fuse_factor: int = 6) -> dict:
    """How close is what the panel actually showed to what the artwork looks like?

    Both sides are FUSED first. Floyd-Steinberg carries colour in the spatial mix of pure primaries,
    so a per-pixel comparison of a dithered frame measures every pixel as fully saturated and says
    nothing.

    ⚠️ The panel is normalised to ITS OWN black and white, because that is what a viewer adapts to —
    the panel's white IS white to the eye looking at it. The reference is full-range sRGB. So this
    measures whether the panel's rendering is faithful WITHIN the range it can produce; it does not,
    and cannot, measure the range itself.
    """
    r = read_panel(photo, w, h, roi=roi, flat=flat)
    import tools.eink_target as _et
    cx0, cy0, cx1, cy1 = _et.content_box(w, h)
    panel = r["corrected"].crop((cx0, cy0, cx1, cy1))
    ref = ref.convert("RGB").resize(panel.size, Image.LANCZOS)

    def _fuse(im):
        return np.asarray(im.resize((im.width // fuse_factor, im.height // fuse_factor),
                                    Image.BOX)).astype(float)
    P, R = _fuse(panel), _fuse(ref)

    def _hsv(a):
        x = np.asarray(Image.fromarray(a.astype(np.uint8), "RGB").convert("HSV")).astype(float)
        return x[..., 0], x[..., 1], x[..., 2]
    ph, ps, pv = _hsv(P)
    rh, rs, rv = _hsv(R)
    dh = np.abs(ph - rh) % 256.0
    dh = np.minimum(dh, 256.0 - dh)
    # HIGHLIGHT DETAIL RETENTION — the thing RMS cannot see.
    # White-point compression exists because content above the white ink's luminance (163) has no ink
    # to be built from and collapses to flat white. That is a loss of local STRUCTURE, and a fused
    # pixel-difference metric is blind to it: a flat white region and a correctly textured one can
    # have similar means. So measure local variation directly, in exactly the region at risk.
    hi = rv > 163.0
    if hi.sum() > 40:
        def _local_sd(x):
            k = 3
            xp = np.pad(x, k, mode="edge")
            acc = np.zeros_like(x)
            acc2 = np.zeros_like(x)
            n = 0
            for dy in range(-k, k + 1):
                for dx in range(-k, k + 1):
                    w_ = xp[k + dy:k + dy + x.shape[0], k + dx:k + dx + x.shape[1]]
                    acc += w_
                    acc2 += w_ * w_
                    n += 1
            mean = acc / n
            return np.sqrt(np.maximum(acc2 / n - mean * mean, 0.0))
        sd_ref = _local_sd(rv)[hi].mean()
        sd_pan = _local_sd(pv)[hi].mean()
        detail = float(sd_pan / max(sd_ref, 1e-6))
    else:
        detail = float("nan")
    return {
        "highlight_detail": detail,
        "d_luminance": float((pv - rv).mean()),
        "abs_luminance": float(np.abs(pv - rv).mean()),
        "d_saturation": float((ps - rs).mean()),
        "abs_hue": float(dh.mean()),
        "rms": float(np.sqrt(((P - R) ** 2).mean())),
        "patch_residual": r["patch_residual"],
    }


def cmd_score(args) -> None:
    w, h = args.width, args.height
    flat = build_flat_field(Image.open(args.flat), w, h) if args.flat else None
    m = score_against_reference(Image.open(args.photo), Image.open(args.reference), w, h, flat=flat)
    print(f"  d_luminance {m['d_luminance']:+7.2f}   |d_lum| {m['abs_luminance']:6.2f}")
    print(f"  d_saturation{m['d_saturation']:+7.2f}   |d_hue| {m['abs_hue']:6.2f}")
    print(f"  RMS {m['rms']:6.2f}    patch non-uniformity {m['patch_residual']:.2f}")
    print(f"  highlight detail retained {m['highlight_detail']:.3f}  "
          f"(1.0 = same local structure as the reference above the ink ceiling; 0 = flattened)")


def cmd_lock(args) -> None:
    """Pin the camera so nothing drifts between measurements, then VERIFY it took.

    Auto exposure, auto white balance and autofocus each re-decide per frame, so with them on, two
    photographs of the same panel are two different measurements. Focus must be set AFTER disabling
    continuous autofocus — the control is inactive until then and the write fails with 'Permission
    denied', which looks like a permissions problem and is not one.
    """
    dev = args.device
    lock = [(c, args.exposure if (c == "exposure_time_absolute" and args.exposure) else v)
            for c, v in CAMERA_LOCK]
    for ctrl, val in lock:
        _v4l2_set(dev, ctrl, val)
    _v4l2_set(dev, "focus_automatic_continuous", 0)
    _v4l2_set(dev, "focus_absolute", args.focus)
    _v4l2_set(dev, *GAIN_CTRL)
    print("locked camera controls (read back):")
    bad = []
    for ctrl, val in lock + [("focus_automatic_continuous", 0), ("focus_absolute", args.focus)]:
        got = _v4l2_get(dev, ctrl)
        ok = got == val
        if not ok:
            bad.append((ctrl, val, got))
        print(f"  {ctrl:32s} want {val:5} got {got}  {'ok' if ok else '<-- DID NOT TAKE'}")
    if bad:
        print("\n⚠️ some controls did not take. Read-back is not optional here: this camera "
              "silently overrode gain and exposure during setup, and the only symptom was clipped "
              "measurements that looked like a lighting problem.")


def cmd_capture(args) -> None:
    """Grab a frame, optionally waiting until the PANEL has stopped changing.

    ⚠️ AN E-INK REFRESH IS NOT INSTANT AND IT IS NOT MONOTONIC. A Spectra 6 update takes ~9-16 s and
    drives the pixels through inversion and flashing phases on the way, so a frame grabbed too early
    is not a slightly-early version of the final image — it is a DIFFERENT image. The first real
    capture of this rig was taken mid-refresh and produced a patch residual of 97/255 and a negative
    blue gain, which reads exactly like a badly mis-calibrated camera rather than like a timing bug.

    So settle by OBSERVATION rather than by a fixed sleep: keep grabbing until two consecutive frames
    agree, which adapts to whatever the panel and the lighting are actually doing. A fixed delay
    would be both slower on average and wrong exactly when the panel is slowest.
    """
    if not args.settle:
        _grab(args.device, args.size, args.warmup, args.out)
        print(f"captured -> {args.out}")
        return
    tmp_a = Path(args.out).with_suffix(".settle_a.png")
    tmp_b = Path(args.out).with_suffix(".settle_b.png")
    _grab(args.device, args.size, args.warmup, str(tmp_a))
    stable = 0
    for attempt in range(1, args.settle_tries + 1):
        _grab(args.device, args.size, max(2, args.warmup // 4), str(tmp_b))
        delta = _frame_delta(tmp_a, tmp_b)
        stable = stable + 1 if delta <= args.settle_delta else 0
        print(f"  settle {attempt}: frame delta {delta:6.2f}  stable {stable}/{args.settle_stable}")
        if stable >= args.settle_stable:
            tmp_b.replace(Path(args.out))
            tmp_a.unlink(missing_ok=True)
            print(f"captured (settled) -> {args.out}")
            return
        tmp_b.replace(tmp_a)
    tmp_a.replace(Path(args.out))
    print(f"⚠️ never settled below {args.settle_delta} in {args.settle_tries} tries — "
          f"kept the last frame. The panel may still be refreshing, or something in shot is moving.")


def cmd_read(args) -> None:
    photo = Image.open(args.photo)
    w, h = (Image.open(args.target).size if args.target else (args.width, args.height))
    roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else None
    flat = None
    if args.flat:
        flat = build_flat_field(Image.open(args.flat), w, h, roi=roi)
        print(f"flat-field applied from {args.flat} "
              f"(illumination range {flat.min():.0f}-{flat.max():.0f})")
    r = read_panel(photo, w, h, roi=roi, flat=flat)
    print(f"gain   {[round(float(v), 4) for v in r['gain']]}")
    print(f"offset {[round(v, 2) for v in r['offset']]}")
    print(f"patch non-uniformity (mean std within patches, 0-255): {r['patch_residual']:.2f}")
    if r["patch_residual"] > 14:
        print("  ⚠️ high — uneven light or a specular highlight on the glass. Both bias every "
              "later measurement, so fix the lighting rather than correcting for it.")
    out = Path(args.out or "bench-eink/measured_corrected.png")
    r["corrected"].save(out)
    print(f"corrected -> {out}")
    if args.primaries:
        print("\nMEASURED PANEL PRIMARIES vs the assumed SPECTRA6_DITHER_PALETTE")
        print("(both in PANEL-RELATIVE units: this panel's own black = 0, its own white = 255)")
        prim = measured_primaries(r["corrected"], w, h)
        assumed_norm = normalise_palette(ep.SPECTRA6_DITHER_PALETTE)
        worst = 0.0
        for name, assumed in zip(et.INK_NAMES, assumed_norm):
            got = prim[name]
            d = max(abs(a - b) for a, b in zip(got, assumed))
            worst = max(worst, d)
            flag = "  <-- DIFFERS" if d > 30 else ""
            print(f"  {name:7s} measured {[round(v) for v in got]}   assumed {[round(v) for v in assumed]}"
                  f"   worst channel {d:5.1f}{flag}")
        print(f"\n  largest disagreement: {worst:.1f}/255")
        if worst > 30:
            print("  This panel does not match Pimoroni's measurement, and every distance "
                  "calculation in the renderer assumes it does.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="validate the pipeline against synthetic photographs")
    sc = sub.add_parser("score", help="fidelity of a photographed panel against its reference")
    sc.add_argument("photo")
    sc.add_argument("reference")
    sc.add_argument("--flat", default="")
    sc.add_argument("--width", type=int, default=1600)
    sc.add_argument("--height", type=int, default=1200)

    lk = sub.add_parser("lock", help="pin camera exposure/WB/focus/gain and verify it took")
    lk.add_argument("--device", default="/dev/video0")
    lk.add_argument("--focus", type=int, default=30)
    lk.add_argument("--exposure", type=int, default=None,
                    help="override exposure_time_absolute; re-sweep this whenever the light changes")
    c = sub.add_parser("capture", help="grab one frame from a v4l2 device")
    c.add_argument("--device", default="/dev/video0")
    c.add_argument("--size", default="1920x1080")
    c.add_argument("--warmup", type=int, default=12, help="frames to discard so AE/AWB settle")
    c.add_argument("--out", default="bench-eink/shot.png")
    c.add_argument("--settle", action="store_true",
                   help="keep grabbing until two consecutive frames agree — an e-ink refresh passes "
                        "through inversion phases, so an early grab is a different image, not an "
                        "early one")
    c.add_argument("--settle-delta", type=float, default=1.2,
                   help="mean abs frame difference (0-255) counted as settled")
    c.add_argument("--settle-tries", type=int, default=25)
    c.add_argument("--settle-stable", type=int, default=3,
                   help="consecutive agreeing frames required. ONE agreement is not enough: a Spectra "
                        "6 refresh passes through slow phases where two successive grabs look "
                        "identical, and a flat-field reference was once captured mid-refresh as a "
                        "dark purple inversion state that then corrupted everything divided by it.")
    r = sub.add_parser("read", help="rectify + normalise a photograph and report")
    r.add_argument("photo")
    r.add_argument("--target", default="", help="the rendered target, to take w/h from")
    r.add_argument("--width", type=int, default=1600)
    r.add_argument("--height", type=int, default=1200)
    r.add_argument("--roi", default="", help="x0,y0,x1,y1 crop to the panel's active area")
    r.add_argument("--flat", default="", help="photograph of an all-white panel; divides out the "
                                              "lighting gradient and lens vignetting")
    r.add_argument("--primaries", action="store_true", help="report measured ink primaries")
    r.add_argument("--out", default="")
    args = ap.parse_args()
    {"selftest": cmd_selftest, "capture": cmd_capture, "read": cmd_read,
     "lock": cmd_lock, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    main()
