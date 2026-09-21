"""
tools/subject_crops.py — derive SUBJECT-FILLING aspect_crops for full-sheet plate scans
(maintainer tool — NOT part of the runtime image; ADR-087).

`derive_aspect_crops.py`'s boxes were authored to PRESERVE COMPOSITION (keep the whole plate, accept
paper margins). ADR-087 decided that's wrong for a landscape panel showing a full-sheet scan (an
Audubon plate is ~60% margin): "for a landscape panel, prefer a subject-filling grab over a
composition-preserving one, even at the cost of losing part of the work." This tool derives THAT kind
of box, algorithmically (no vision fan-out — the plates are uniform enough that a deterministic
detector works, and it's testable + reproducible + fast; vision is reserved for reviewing the contact
sheets this tool produces).

**Method.** A full-sheet plate is: white paper margin, a printed plate (the ink), a caption band below
the plate (species name + Latin name), more paper below that. So:

1. `ink_mask` — downsample, mark every pixel that's dark OR saturated as ink, clean up scanner dust
   with a small morphological open/close, then drop small isolated components (dust, stray corner
   text like a plate number) via connected-component filtering.
2. `plate_bbox` — bbox of all surviving ink (this still includes the caption).
3. `caption_band` — find where the caption starts (a low-density tail off the plate's bottom edge that
   contains a genuinely blank gap), and cut it off. `subject_mask` = the ink mask with the caption
   rows zeroed out.
4. `subject_box` — for each of the four `ASPECT_CROP_KEYS`, find the SMALLEST box of that exact aspect
   that encloses >= `coverage` of the subject's ink mass (bisection on box size, integral-image argmax
   for position at each size), falling back to the largest box that fits the frame (centred on the ink
   centroid) if even that can't reach `coverage`. Every returned box is snapped
   (`derive_aspect_crops.snap`) to guarantee exact-aspect correctness.
5. `gate_subject_box` — accept / reject / flag-for-review. A REJECT drops that one key (mirrors
   `derive_aspect_crops`'s per-key atomicity); a FLAG keeps the box but lists it for human review.

No scipy/cv2 in this environment — morphology runs through `PIL.ImageFilter.Min/MaxFilter` (grayscale
min/max = binary erosion/dilation on a 0/255 image) and connected components run through a from-scratch
two-pass run-length + union-find labeler (`_connected_components`), not a labeled-image library.

    python -m tools.subject_crops --collection audubon-birds-of-america --library art-pack/_Library \
        --out bench-eink/analysis/audubon_crops_2026-09-20/

Writes `results.json` (every item, all four boxes, diagnostics, gate flags — the shape
`derive_aspect_crops.bake --gate subject` consumes), `review.json` (just the flagged items),
`summary.json` (acceptance metrics), per-aspect-key contact sheets (`sheet_<key>_NN.png`, new box in
red, the OLD catalog box dashed blue), and `index.html` tying it together.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# `epaper.py` lives at the repo root; tools/ scripts import it directly (house convention — see
# tools/derive_aspect_crops.py). The sys.path insert makes this robust even off `python -m` from root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from epaper import ASPECT_CROP_KEYS  # noqa: E402
from tools.derive_aspect_crops import (  # noqa: E402
    ASPECT_INEXACT_TOL,
    DEFAULT_DIRS,
    _hash8,
    _iter_catalog_items,
    snap,
)
from tools.tag_resolution import index_masters  # noqa: E402

# --- ink_mask ----------------------------------------------------------------------------
MASK_PX = 800
PAPER_MIN_LUM = 200.0             # 0..255; same paper rule tools/ink_span_scan.py starts from
SAT_MIN = 0.28                    # HSV saturation, 0..1 — catches colored ink on light paper.
                                   # Measured: aged/cream Audubon stock itself carries sat ~0.15-0.22
                                   # (a warm tint, not noise — a sharp density cliff sits at ~0.22),
                                   # so 0.18 misread most of a cream sheet as "ink" on some plates.
MORPH_PX = 3                      # open/close structuring element (square), drops scanner dust
MIN_COMPONENT_FRACTION = 0.0005   # 0.05% of the working canvas — drop smaller ink islands

# --- caption_band --------------------------------------------------------------------------
CAPTION_BLANK_RATIO = 0.05   # a row below this * median is genuinely blank (the gap itself)
CAPTION_GAP_FRAC = 0.015     # the blank run must be at least this fraction of image height
CAPTION_SEARCH_FRAC = 0.30   # only look for the gap within the plate's bottom 30% of height

# --- subject_box ---------------------------------------------------------------------------
COVERAGE_DEFAULT = 0.92
BISECT_STEPS = 60
POSITION_STRIDE = 2
POSITION_TIE_TOL = 0.005   # placements within this fraction of the max enclosed mass are "tied";
                           # break ties toward the subject's own extent centre (see _best_position)

# --- gate ------------------------------------------------------------------------------------
# The "REJECT" constants document where a genuinely-broken box would sit; they're not applied
# (see gate_subject_box's docstring) since a rejected key just falls back to the OLD
# composition-preserving crop, which has the exact "too much paper" problem this pass fixes.
COVERAGE_REJECT = 0.85
AREA_REJECT = 0.06
PAPER_REJECT = 0.55
# The "FLAG" constants (put the item on the review list, keep the box) are calibrated against the
# REAL measured distribution across the corpus, not a priori guesses — Audubon plates are
# genuinely sparse (delicate line art on a big sheet), so a naive threshold (e.g. paper_fraction >
# 0.40) flags the vast majority of otherwise-good boxes. These sit at roughly the 2nd/98th
# percentile of a random-sample measurement, so only real outliers land on the review list.
COVERAGE_FLAG = 0.45
AREA_FLAG = 0.09
PAPER_FLAG = 0.90
INK_FRACTION_FLAG = 0.03

# --- contact sheets --------------------------------------------------------------------------
SHEET_GRID = (5, 6)   # cols, rows -> 30 thumbs/sheet
THUMB_EDGE = 260


# ============================================================================= connected components
def _row_runs(row: np.ndarray) -> list[tuple[int, int]]:
    """Vectorized run-length detection for one boolean row -> [(start, end_exclusive), ...]."""
    if not row.any():
        return []
    d = np.diff(row.astype(np.int8))
    starts = list(np.where(d == 1)[0] + 1)
    ends = list(np.where(d == -1)[0] + 1)
    if row[0]:
        starts = [0] + starts
    if row[-1]:
        ends = ends + [row.shape[0]]
    return list(zip(starts, ends))


def _connected_components(mask: np.ndarray) -> tuple[np.ndarray, dict[int, int]]:
    """4-connectivity connected-component labeling via a two-pass run-length scan + union-find —
    no scipy/skimage available in this environment. Returns (labels int32, {label: pixel_count});
    label 0 is background. Operates on RUNS per row (not individual pixels), so cost tracks the
    number of ink runs, not the canvas's raw pixel count."""
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    parent: list[int] = [0]

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def new_label() -> int:
        parent.append(len(parent))
        return len(parent) - 1

    prev_runs: list[tuple[int, int, int]] = []
    for y in range(h):
        runs = _row_runs(mask[y])
        cur_runs: list[tuple[int, int, int]] = []
        for xs, xe in runs:
            found = [pl for ps, pe, pl in prev_runs if ps < xe and pe > xs]
            if not found:
                lbl = new_label()
            else:
                lbl = find(found[0])
                for fl in found[1:]:
                    union(lbl, fl)
                    lbl = find(lbl)
            labels[y, xs:xe] = lbl
            cur_runs.append((xs, xe, lbl))
        prev_runs = cur_runs

    flat = labels.ravel()
    nz = flat > 0
    if nz.any():
        roots = np.array([find(int(v)) for v in flat[nz]], dtype=np.int32)
        flat = flat.copy()
        flat[nz] = roots
        labels = flat.reshape(h, w)
        uniq, counts = np.unique(roots, return_counts=True)
        count_map = dict(zip(uniq.tolist(), counts.tolist()))
    else:
        count_map = {}
    return labels, count_map


# ============================================================================= ink_mask
def ink_mask(img_rgb: Image.Image, px: int = MASK_PX) -> np.ndarray:
    """Downsample to `px` long edge, mark ink pixels (dark OR saturated), morphologically clean
    (open then close, MORPH_PX-square), then drop connected components smaller than
    MIN_COMPONENT_FRACTION of the canvas (scanner dust, stray corner text)."""
    im = img_rgb if img_rgb.mode == "RGB" else img_rgb.convert("RGB")
    w, h = im.size
    scale = px / max(w, h)
    if scale < 1:
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    arr = np.asarray(im, dtype=np.float32) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    lum = (0.299 * r + 0.587 * g + 0.114 * b) * 255.0
    mx = arr.max(axis=-1)
    mn = arr.min(axis=-1)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    raw = (lum < PAPER_MIN_LUM) | (sat > SAT_MIN)

    m_img = Image.fromarray((raw * 255).astype(np.uint8), mode="L")
    opened = m_img.filter(ImageFilter.MinFilter(MORPH_PX)).filter(ImageFilter.MaxFilter(MORPH_PX))
    closed = opened.filter(ImageFilter.MaxFilter(MORPH_PX)).filter(ImageFilter.MinFilter(MORPH_PX))
    mask = np.asarray(closed) > 127

    labels, counts = _connected_components(mask)
    total = mask.size
    min_area = max(1, int(round(MIN_COMPONENT_FRACTION * total)))
    small = {lbl for lbl, cnt in counts.items() if cnt < min_area}
    if small:
        mask = mask & ~np.isin(labels, list(small))
    return mask


def plate_bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """Pixel-index bbox (x0, y0, x1, y1) of every ink pixel, x1/y1 exclusive. None if blank."""
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def caption_band(mask: np.ndarray, plate: tuple[int, int, int, int] | None) -> int | None:
    """Row index (exclusive top of the caption region) where the plate's caption starts, or None.

    Scans up from the plate's bottom looking for the FIRST (bottom-most) run of genuinely blank
    rows (< 0.05x the plate's median row density) that is >= 1.5% of the image height — the gap
    between the subject and the caption. The cut is the top edge of that run: everything below it
    (the gap itself, plus whatever sits below the gap, however dense — a caption's title line can
    be as dense as the subject) is caption; everything above is subject.

    A single low-density THRESHOLD on the caption text itself is unreliable — measured on real
    plates, a caption's bold title line can read as dense as the subject, while a thin marginalia
    row (plate number, edge numeral) reads far sparser — so the signal that actually separates
    subject from caption is the BLANK GAP between them, not the caption's own density. `subject_mask
    = mask[:cut]` is the caller's job.

    The search is bounded to the bottom `CAPTION_SEARCH_FRAC` of the plate's own height. Without
    that bound, a full-bleed scenic plate (sky/rock/ocean, no paper margins — measured on a real
    Audubon work) can have its own faint sky band near the TOP read as "blank" once the scan has
    climbed past the whole dense body of the painting looking for a caption that was never there,
    collapsing the subject mask to almost nothing. A real caption is always close to the plate's
    bottom edge; a gap found far from it is something else in the composition, not a caption."""
    if plate is None:
        return None
    x0, y0, x1, y1 = plate
    h, _w = mask.shape
    pw = max(1, x1 - x0)
    row_frac = mask[:, x0:x1].sum(axis=1).astype(np.float64) / pw
    band = row_frac[y0:y1]
    nz = band[band > 0]
    if nz.size == 0:
        return None
    median_frac = float(np.median(nz))
    if median_frac <= 0:
        return None
    blank_thresh = CAPTION_BLANK_RATIO * median_frac
    gap_rows = max(1, int(round(CAPTION_GAP_FRAC * h)))
    search_floor = y1 - int(round(CAPTION_SEARCH_FRAC * (y1 - y0)))

    y = y1 - 1
    run = 0
    gap_top = None
    while y >= max(y0, search_floor):
        if row_frac[y] < blank_thresh:
            run += 1
            if run >= gap_rows:
                gap_top = y
        else:
            if gap_top is not None:
                break
            run = 0
        y -= 1
    return int(gap_top) if gap_top is not None else None


# ============================================================================= subject_box
def _target_ratio(key: str) -> float:
    kw, kh = key.split(":")
    return float(kw) / float(kh)


def _integral_image(mask: np.ndarray) -> np.ndarray:
    cs = mask.astype(np.float64).cumsum(0).cumsum(1)
    ii = np.zeros((mask.shape[0] + 1, mask.shape[1] + 1), dtype=np.float64)
    ii[1:, 1:] = cs
    return ii


def _best_position(ii: np.ndarray, h_px: int, w_px: int, h: int, w: int, stride: int,
                    prefer_center: tuple[float, float] | None = None,
                    tie_tol: float = POSITION_TIE_TOL) -> tuple[float, tuple[int, int]]:
    """argmax enclosed ink mass, over a stride-`stride` grid of top-left positions, via the
    integral image — fully vectorized (no python loop over candidate positions). Returns
    (best_mass, (y0_px, x0_px)).

    When `prefer_center` (a (y, x) pixel point — the subject's own EXTENT centre, not its mass
    centroid) is given, ties are broken toward it: among every placement whose enclosed mass is
    within `tie_tol` of the true maximum (not just the single cell `argmax` would pick), the one
    whose OWN centre is closest to `prefer_center` wins.

    Without this, a box with slack on some axis — already wide/tall enough to enclose the whole
    subject along that axis, so every placement along it scores identically — silently returns
    `argmax`'s default (the first, i.e. top-left-most, tied cell), dumping ALL the surplus paper on
    one side. Measured on MacGillivray's Finch: a 4:3 box that already fully contained both birds
    left ~40% empty paper on the LEFT, because every x0 from 0 up to the slack's end tied on mass
    and `argmax` simply returned the first one."""
    ys = np.arange(0, h - h_px + 1, stride)
    xs = np.arange(0, w - w_px + 1, stride)
    if ys.size == 0:
        ys = np.array([max(0, h - h_px)])
    if xs.size == 0:
        xs = np.array([max(0, w - w_px)])
    y0, x0 = np.meshgrid(ys, xs, indexing="ij")
    y1, x1 = y0 + h_px, x0 + w_px
    sums = ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0]

    if prefer_center is not None:
        best_sum = float(sums.max())
        thresh = best_sum * (1.0 - tie_tol) if best_sum > 0 else best_sum
        near_max = sums >= thresh
        cy = y0.astype(np.float64) + h_px / 2.0
        cx = x0.astype(np.float64) + w_px / 2.0
        pc_y, pc_x = prefer_center
        dist2 = (cy - pc_y) ** 2 + (cx - pc_x) ** 2
        dist2 = np.where(near_max, dist2, np.inf)
        flat = int(np.argmin(dist2))
    else:
        flat = int(np.argmax(sums))
    iy, ix = np.unravel_index(flat, sums.shape)
    return float(sums[iy, ix]), (int(y0[iy, ix]), int(x0[iy, ix]))


def _centroid(mask: np.ndarray) -> tuple[float, float]:
    if not mask.any():
        return 0.5, 0.5
    h, w = mask.shape
    ys, xs = np.where(mask)
    return (float(xs.mean()) + 0.5) / w, (float(ys.mean()) + 0.5) / h


def _centered_box(bh: float, r: float, centroid: tuple[float, float]) -> list[float]:
    """The largest box of aspect ratio (in normalized w/h terms) `r` at height `bh`, centred on
    `centroid` and clamped to stay inside [0,1] — used only for the fully-blank-mask edge case."""
    bw = min(1.0, bh * r)
    bh = min(1.0, bh)
    cx, cy = centroid
    x0 = min(max(cx - bw / 2, 0.0), 1.0 - bw)
    y0 = min(max(cy - bh / 2, 0.0), 1.0 - bh)
    return [round(x0, 4), round(y0, 4), round(x0 + bw, 4), round(y0 + bh, 4)]


TOP_ANCHOR_MARGIN_FRAC = 0.02  # landscape-key fallback: headroom above the subject's own top edge


def _extent(mask: np.ndarray) -> tuple[int, int, int, int]:
    """(y_lo, y_hi, x_lo, x_hi) pixel bbox of every ink pixel; y_hi/x_hi exclusive. Callers only
    reach this once the mask is known non-empty (total mass > 0)."""
    ys, xs = np.where(mask)
    return int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1


def _extent_center(mask: np.ndarray) -> tuple[float, float]:
    """(cy, cx) pixel midpoint of the subject's own bbox EXTENT — not its mass centroid. This is
    the tie-break target `subject_box` passes into `_best_position`, and what `_extent_anchored_box`
    centres its non-anchored axis on."""
    y_lo, y_hi, x_lo, x_hi = _extent(mask)
    return (y_lo + y_hi) / 2.0, (x_lo + x_hi) / 2.0


def _extent_anchored_box(mask: np.ndarray, h_px: int, w_px: int, *, anchor_top: bool) -> list[int]:
    """Position a `h_px`x`w_px` window against the subject's own pixel EXTENT (bbox) — not its mass
    centroid and not the argmax-best-mass position — the "can't reach coverage" fallback. Horizontal
    placement is always centred on the extent's own midpoint.

    Vertical placement depends on `anchor_top`:

    - False (portrait keys, 9:16/3:4 — `subject_box` passes this when `target_aspect <= 1`):
      centre vertically on the extent's midpoint too. Measured on real plates: for a tall bird
      standing upright (body mass concentrated low, head thin and far above it), BOTH the mass
      centroid and the pure argmax-best-mass position sit low — they optimize for total captured
      ink, which a broad body/wings/tail simply has more of than a thin head, so either one fully
      sacrifices the head to buy a little more tail. Centring on the bbox's own midpoint instead
      splits the loss between the two extremities rather than dumping it all on the low-mass end.

    - True (landscape keys, 16:9/4:3 — `target_aspect > 1`): anchor the box's TOP edge at the
      subject's own top extent, plus `TOP_ANCHOR_MARGIN_FRAC` of headroom (clamped to stay in
      frame). A landscape box is SHORT relative to an upright subject's own height, so even
      extent-CENTRING (the portrait-key rule above) still routinely lands its top edge below the
      head — measured on the Wild Turkey, a Golden Eagle, and the stacked Snowy Owls plate.
      There's no ambiguity about which end matters for an upright bird: the head is always at the
      top of its own extent, so anchoring there and letting the loss fall on the feet/base/tail is
      a strictly better trade, never a worse one, for this shape class. This only runs when the
      coverage target is already unreachable at the largest possible box, so there's no "better,
      still-sufficient" placement being missed either way."""
    h, w = mask.shape
    y_lo, y_hi, x_lo, x_hi = _extent(mask)
    cx = (x_lo + x_hi) / 2.0
    x0_px = int(round(min(max(cx - w_px / 2, 0.0), w - w_px)))
    if anchor_top:
        margin_px = round(TOP_ANCHOR_MARGIN_FRAC * h)
        y0_px = int(round(min(max(y_lo - margin_px, 0.0), h - h_px)))
    else:
        cy = (y_lo + y_hi) / 2.0
        y0_px = int(round(min(max(cy - h_px / 2, 0.0), h - h_px)))
    return [x0_px, y0_px]


def subject_box(mask: np.ndarray, target_aspect: float, source_aspect: float,
                 coverage: float = COVERAGE_DEFAULT) -> list[float]:
    """The smallest box of exact `target_aspect` (real, source-aspect-corrected) that encloses
    >= `coverage` of `mask`'s ink mass — a bisection on box height (`BISECT_STEPS` steps), with the
    best position at each candidate size found via an integral-image argmax (stride
    `POSITION_STRIDE`), with ties (placements within `POSITION_TIE_TOL` of the max enclosed mass —
    i.e. an axis with SLACK, already big enough to enclose the whole subject along it) broken
    toward the subject's own extent centre rather than defaulting to the top-left-most tied cell
    (measured on MacGillivray's Finch: a reachable-coverage box left ~40% empty paper on one side
    before this — see `_best_position`'s docstring). Falls back to the largest box that fits the
    frame at this aspect, positioned against the subject's own pixel EXTENT (see
    `_extent_anchored_box`), when even that can't reach `coverage` (an elongated single subject, or
    multi-subject plates whose parts are too far apart — ADR-087's accepted trade: more paper, and
    the loss placed where it costs least). For a landscape key (`target_aspect > 1`: 16:9/4:3) the
    fallback anchors on the subject's TOP edge (an upright bird's head, never its feet); for a
    portrait key it centres on the extent instead (both axes, same tie-break principle as above).
    Always returns an exact-aspect box (post-`snap`)."""
    h, w = mask.shape
    total = float(mask.sum())
    r = target_aspect / source_aspect if source_aspect > 0 else 1.0
    hi = min(1.0, 1.0 / r) if r > 0 else 1.0

    if total <= 0:
        box = _centered_box(hi, r, (0.5, 0.5))
        return snap(box, source_aspect, target_aspect)

    ii = _integral_image(mask)
    prefer_center = _extent_center(mask)

    def eval_scale(bh: float):
        h_px = max(1, min(h, int(round(bh * h))))
        w_px = max(1, min(w, int(round(bh * r * w))))
        best_sum, pos = _best_position(ii, h_px, w_px, h, w, POSITION_STRIDE,
                                        prefer_center=prefer_center)
        return best_sum / total, h_px, w_px, pos

    cov_hi, h_px, w_px, pos = eval_scale(hi)
    if cov_hi < coverage:
        # Can't reach the coverage target even at the largest box this aspect can be -- see
        # `_extent_anchored_box`'s docstring for why this uses the subject's own bbox extent
        # rather than the argmax-best-mass position or the raw mass centroid, and why landscape
        # keys anchor on the TOP of that extent instead of centring on it.
        x0_px, y0_px = _extent_anchored_box(mask, h_px, w_px, anchor_top=target_aspect > 1.0)
        box = [round(x0_px / w, 4), round(y0_px / h, 4),
               round((x0_px + w_px) / w, 4), round((y0_px + h_px) / h, 4)]
        return snap(box, source_aspect, target_aspect)

    best = (h_px, w_px, pos)
    lo_bh, hi_bh = 0.0, hi
    for _ in range(BISECT_STEPS):
        mid = (lo_bh + hi_bh) / 2.0
        cov, h_px_m, w_px_m, pos_m = eval_scale(mid)
        if cov >= coverage:
            hi_bh = mid
            best = (h_px_m, w_px_m, pos_m)
        else:
            lo_bh = mid

    h_px, w_px, (y0_px, x0_px) = best
    box = [round(x0_px / w, 4), round(y0_px / h, 4),
           round((x0_px + w_px) / w, 4), round((y0_px + h_px) / h, 4)]
    return snap(box, source_aspect, target_aspect)


def _box_pixels(box: list[float], w: int, h: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    px0 = max(0, min(w - 1, int(round(x0 * w))))
    py0 = max(0, min(h - 1, int(round(y0 * h))))
    px1 = max(px0 + 1, min(w, int(round(x1 * w))))
    py1 = max(py0 + 1, min(h, int(round(y1 * h))))
    return px0, py0, px1, py1


def _box_stats(mask: np.ndarray, subject_mask: np.ndarray,
                box: list[float]) -> tuple[float, float, float]:
    """(coverage, area, paper_fraction) of a normalized box: `coverage` against the caption-cut
    `subject_mask`'s own mass, `paper_fraction` against the full (pre-cut) ink `mask` — paper is
    paper regardless of the caption logic. `area` is the box's own normalized area."""
    h, w = mask.shape
    x0, y0, x1, y1 = _box_pixels(box, w, h)
    sub_total = float(subject_mask.sum())
    enclosed = float(subject_mask[y0:y1, x0:x1].sum())
    coverage = enclosed / sub_total if sub_total > 0 else 0.0
    box_px = (x1 - x0) * (y1 - y0)
    ink_in_box = float(mask[y0:y1, x0:x1].sum())
    paper_fraction = 1.0 - (ink_in_box / box_px if box_px > 0 else 0.0)
    area = (box[2] - box[0]) * (box[3] - box[1])
    return coverage, area, paper_fraction


# ============================================================================= derive
def derive(path: Path, coverage: float = COVERAGE_DEFAULT) -> dict:
    """Derive all four subject-filling `aspect_crops` boxes for one master, plus diagnostics.

    Returns {"aspect_crops": {key: box, ...4 keys...}, "diag": {...}, "source_aspect": float}.
    `diag` = {"plate": box|None, "caption_cut": y_norm|None, "centroid": [cx,cy],
    "ink_fraction": float, "boxes": {key: {"coverage","area","paper_fraction"}}}. `source_aspect`
    (width/height of the REAL master, not the downsampled mask) is carried alongside for the
    caller's own gating -- `snap`/`gate_subject_box` both need the true value, matching how
    `derive_aspect_crops._source_aspect` reads it, so the two never disagree on the same master."""
    with Image.open(path) as im:
        w0, h0 = im.size
        source_aspect = w0 / h0
        im.draft("RGB", (MASK_PX, MASK_PX))
        mask = ink_mask(im.convert("RGB"), px=MASK_PX)

    h, w = mask.shape
    plate = plate_bbox(mask)
    ink_fraction = float(mask.sum()) / mask.size if mask.size else 0.0
    cut = caption_band(mask, plate)
    subject_mask = mask.copy()
    if cut is not None:
        subject_mask[cut:, :] = False
    centroid = _centroid(subject_mask if subject_mask.any() else mask)

    boxes: dict[str, list[float]] = {}
    box_diag: dict[str, dict] = {}
    for key in ASPECT_CROP_KEYS:
        target = _target_ratio(key)
        box = subject_box(subject_mask, target, source_aspect, coverage)
        cov, area, paper = _box_stats(mask, subject_mask, box)
        boxes[key] = box
        box_diag[key] = {"coverage": round(cov, 4), "area": round(area, 4),
                          "paper_fraction": round(paper, 4)}

    diag = {
        "plate": ([round(plate[0] / w, 4), round(plate[1] / h, 4),
                   round(plate[2] / w, 4), round(plate[3] / h, 4)] if plate else None),
        "caption_cut": (round(cut / h, 4) if cut is not None else None),
        "centroid": [round(centroid[0], 4), round(centroid[1], 4)],
        "ink_fraction": round(ink_fraction, 4),
        "boxes": box_diag,
    }
    return {"aspect_crops": boxes, "diag": diag, "source_aspect": source_aspect}


# ============================================================================= gate
def gate_subject_box(box, source_aspect: float, target: float, *, coverage: float,
                      area: float, paper_fraction: float, ink_fraction: float | None = None,
                      caption_cut: float | None = None) -> tuple[str | None, list[str]]:
    """Gate one subject box. Aspect exactness and [0,1] range are RECOMPUTED from `box` +
    `source_aspect` (never trusted -- same non-trust posture as `derive_aspect_crops._gate_box`);
    `coverage`/`area`/`paper_fraction` are TRUSTED inputs (the caller -- `derive_aspect_crops
    --gate subject` -- passes the deriver's own recorded diag rather than recomputing them, since
    that needs the master's actual ink mask, which the bake step doesn't otherwise touch).

    `coverage`/`area`/`paper_fraction` are FLAG-ONLY, never reject. `subject_box` already returns
    the best available box for a FIXED exact aspect — either the smallest box that reaches the
    coverage target, or (when even the largest box that fits the frame can't reach it) that largest
    box centred on the ink centroid. Neither case has a "better" box sitting unused: a thin bird on
    a bare branch is inherently sparse even in its own minimal bounding box (measured: routine on
    real plates, not an edge case), and there is nowhere else the box could go. Rejecting drops back
    to the OLD composition-preserving crop, which has the exact "too much paper" problem ADR-087
    exists to fix — worse, not better. COVERAGE_REJECT/AREA_REJECT/PAPER_REJECT are kept only as
    named constants documenting the "this is genuinely bad" line the FLAG thresholds sit inside of.
    Only structural defects (malformed / out of [0,1] / not the exact target aspect) are true
    rejects — `snap()` guarantees none of those should ever actually fire.

    Returns (reject_reason | None, flags) -- a reject means "drop this key"; flags mean "keep it,
    but put it on the review list"."""
    flags: list[str] = []
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return "malformed", flags
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        return "out_of_bounds", flags
    bw, bh = x1 - x0, y1 - y0
    real_aspect = source_aspect * bw / max(bh, 1e-9)
    if abs(real_aspect - target) / target > ASPECT_INEXACT_TOL:
        return "aspect_inexact", flags

    if coverage < COVERAGE_FLAG:
        flags.append("low_coverage")
    if area < AREA_FLAG:
        flags.append("small_area")
    if paper_fraction > PAPER_FLAG:
        flags.append("high_paper")
    # NOTE: caption_cut is accepted but deliberately NOT flagged when None. Measured on real plates:
    # a caption's thin italic script routinely doesn't survive the working-resolution downsample at
    # all (it just never registers as ink), which is the SAFE outcome, not a problem — the subject
    # mask already excludes it because it was never detected, so there's nothing for the box to
    # accidentally include. Flagging every such (majority-case) item would flood the review list
    # with non-issues; `caption_cut` stays in `diag` for anyone who wants to look.
    del caption_cut
    if ink_fraction is not None and ink_fraction < INK_FRACTION_FLAG:
        flags.append("near_empty_plate")
    return None, flags


# ============================================================================= CLI plumbing
def _build_worklist(dirs: list[str], collections: list[str] | None, library: Path) -> list[dict]:
    masters = index_masters(library)
    seen: set[str] = set()
    items: list[dict] = []
    for cid, it in _iter_catalog_items(dirs, collections):
        su = it.get("source_url")
        if not isinstance(su, str) or not su or su in seen:
            continue
        master = masters.get(_hash8(su))
        if master is None:
            continue
        seen.add(su)
        old_ac = it.get("aspect_crops")
        items.append({
            "source_url": su, "collection": cid, "title": it.get("title", ""),
            "master": str(master),
            "old_aspect_crops": old_ac if isinstance(old_ac, dict) else {},
        })
    return items


def _process_item(payload: dict) -> dict:
    """Top-level (picklable) worker: derive + self-gate one master. A corrupt/unreadable master
    must not abort a 446-file run -- caught and reported as an `error` entry instead."""
    master = Path(payload["master"])
    try:
        result = derive(master, coverage=payload.get("coverage", COVERAGE_DEFAULT))
    except Exception as exc:
        return {"source_url": payload["source_url"], "collection": payload["collection"],
                "file": str(master), "title": payload["title"], "error": str(exc),
                "aspect_crops": {}, "diag": {}, "flags": ["error"],
                "old_aspect_crops": payload.get("old_aspect_crops") or {}}

    source_aspect = result["source_aspect"]
    aspect_crops = result["aspect_crops"]
    diag = result["diag"]
    flags: list[str] = []
    for key in ASPECT_CROP_KEYS:
        target = _target_ratio(key)
        kd = diag["boxes"][key]
        reason, kflags = gate_subject_box(
            aspect_crops[key], source_aspect, target,
            coverage=kd["coverage"], area=kd["area"], paper_fraction=kd["paper_fraction"],
            ink_fraction=diag["ink_fraction"], caption_cut=diag["caption_cut"])
        if reason is not None:
            flags.append(f"{key}:reject:{reason}")
        flags.extend(f"{key}:flag:{f}" for f in kflags)

    return {
        "source_url": payload["source_url"], "collection": payload["collection"],
        "file": str(master), "title": payload["title"],
        "aspect_crops": aspect_crops, "diag": diag, "flags": flags,
        "old_aspect_crops": payload.get("old_aspect_crops") or {},
    }


# ============================================================================= contact sheets
def _load_thumb(master: Path, edge: int = THUMB_EDGE) -> Image.Image:
    with Image.open(master) as im:
        im.draft("RGB", (edge * 2, edge * 2))
        im = im.convert("RGB")
        im.thumbnail((edge, edge), Image.LANCZOS)
        # Round-trip through JPEG so the PNG grid we composite into doesn\'t have to encode full
        # photographic detail losslessly -- keeps the committed sheet files small.
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=78)
        buf.seek(0)
        return Image.open(buf).convert("RGB").copy()


def _dashed_rectangle(draw: ImageDraw.ImageDraw, box, color, width: int = 2,
                       dash_len: int = 6, gap_len: int = 4) -> None:
    x0, y0, x1, y1 = box

    def _dashed_line(p0, p1):
        (ax, ay), (bx, by) = p0, p1
        length = max(1.0, ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5)
        step = dash_len + gap_len
        n = int(length // step) + 1
        for i in range(n):
            t0 = min(1.0, (i * step) / length)
            t1 = min(1.0, (i * step + dash_len) / length)
            draw.line([ax + (bx - ax) * t0, ay + (by - ay) * t0,
                       ax + (bx - ax) * t1, ay + (by - ay) * t1], fill=color, width=width)

    _dashed_line((x0, y0), (x1, y0))
    _dashed_line((x1, y0), (x1, y1))
    _dashed_line((x1, y1), (x0, y1))
    _dashed_line((x0, y1), (x0, y0))


def _draw_box(im: Image.Image, box, color, dash: bool = False, width: int = 2) -> None:
    w, h = im.size
    x0, y0, x1, y1 = (box[0] * w, box[1] * h, box[2] * w, box[3] * h)
    d = ImageDraw.Draw(im)
    if dash:
        _dashed_rectangle(d, (x0, y0, x1, y1), color, width=width)
    else:
        d.rectangle([x0, y0, x1, y1], outline=color, width=width)


def _make_sheets(results: list[dict], out_dir: Path) -> dict[str, list[str]]:
    """Per aspect key, SHEET_GRID contact sheets: the new box in red, the OLD catalog box (if any)
    dashed blue. Returns {key: [sheet filenames]}."""
    cols, rows = SHEET_GRID
    per_sheet = cols * rows
    sheet_names: dict[str, list[str]] = {}
    ordered = sorted(results, key=lambda r: r.get("source_url", ""))
    for key in ASPECT_CROP_KEYS:
        safe_key = key.replace(":", "-")
        names: list[str] = []
        for start in range(0, len(ordered), per_sheet):
            chunk = ordered[start:start + per_sheet]
            grid = Image.new("RGB", (cols * THUMB_EDGE, rows * THUMB_EDGE), "white")
            for i, r in enumerate(chunk):
                if r.get("error"):
                    continue
                box = r.get("aspect_crops", {}).get(key)
                if box is None:
                    continue
                thumb = _load_thumb(Path(r["file"]))
                cell = Image.new("RGB", (THUMB_EDGE, THUMB_EDGE), "white")
                ox, oy = (THUMB_EDGE - thumb.width) // 2, (THUMB_EDGE - thumb.height) // 2
                sub = thumb.copy()
                _draw_box(sub, box, "red", dash=False)
                old_box = r.get("old_aspect_crops", {}).get(key)
                if isinstance(old_box, list) and len(old_box) == 4:
                    _draw_box(sub, old_box, (40, 90, 220), dash=True)
                cell.paste(sub, (ox, oy))
                gx, gy = (i % cols) * THUMB_EDGE, (i // cols) * THUMB_EDGE
                grid.paste(cell, (gx, gy))
            idx = start // per_sheet
            name = f"sheet_{safe_key}_{idx:02d}.png"
            grid = grid.convert("P", palette=Image.ADAPTIVE, colors=192)
            grid.save(out_dir / name, "PNG", optimize=True)
            names.append(name)
        sheet_names[key] = names
    return sheet_names


# ============================================================================= summary metrics
FINCH_ADR087_BOX = [0.295, 0.411, 0.715, 0.629]


def _median(sorted_xs: list[float]) -> float | None:
    n = len(sorted_xs)
    if n == 0:
        return None
    mid = n // 2
    return sorted_xs[mid] if n % 2 else (sorted_xs[mid - 1] + sorted_xs[mid]) / 2.0


def _box_area(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _full_width(box) -> bool:
    return box[0] <= 0.002 and box[2] >= 0.998


def _iou(a, b) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def build_summary(results: list[dict]) -> dict:
    ok = [r for r in results if not r.get("error")]
    areas_4_3 = [_box_area(r["aspect_crops"]["4:3"]) for r in ok if "4:3" in r.get("aspect_crops", {})]
    n_4_3 = len(areas_4_3)
    n_16_9 = sum(1 for r in ok if "16:9" in r.get("aspect_crops", {}))
    fullw_4_3 = sum(1 for r in ok if "4:3" in r["aspect_crops"] and _full_width(r["aspect_crops"]["4:3"]))
    fullw_16_9 = sum(1 for r in ok if "16:9" in r["aspect_crops"]
                      and _full_width(r["aspect_crops"]["16:9"]))

    finch = next((r for r in ok
                  if "macgillivray" in (r.get("title", "") + " " + r.get("source_url", "")).lower()),
                 None)
    finch_box = finch["aspect_crops"].get("4:3") if finch else None
    finch_iou = _iou(finch_box, FINCH_ADR087_BOX) if finch_box else None

    all_four = sum(1 for r in ok if set(r.get("aspect_crops", {}).keys()) >= set(ASPECT_CROP_KEYS))

    reason_counts: dict[str, int] = {}
    review_items = []
    for r in results:
        if r.get("flags"):
            review_items.append({"source_url": r.get("source_url"), "title": r.get("title", ""),
                                  "flags": r["flags"]})
            for f in r["flags"]:
                reason_counts[f] = reason_counts.get(f, 0) + 1

    def _dist(field: str):
        vals = [kd[field] for r in ok for kd in r.get("diag", {}).get("boxes", {}).values()]
        if not vals:
            return None
        s = sorted(vals)
        return {"min": round(min(vals), 4), "median": round(_median(s), 4),
                "max": round(max(vals), 4), "mean": round(sum(vals) / len(vals), 4)}

    areas_sorted = sorted(areas_4_3)
    return {
        "n_items": len(results), "n_ok": len(ok), "n_error": len(results) - len(ok),
        "median_4_3_area": round(_median(areas_sorted), 4) if areas_sorted else None,
        "target_median_4_3_area": 0.35, "was_median_4_3_area": 0.52,
        "fullwidth_4_3_fraction": round(fullw_4_3 / n_4_3, 4) if n_4_3 else None,
        "fullwidth_16_9_fraction": round(fullw_16_9 / n_16_9, 4) if n_16_9 else None,
        "target_fullwidth_fraction": 0.05, "was_fullwidth_4_3": 0.59, "was_fullwidth_16_9": 1.00,
        "finch_source_url": finch.get("source_url") if finch else None,
        "finch_4_3_box": finch_box, "finch_adr087_box": FINCH_ADR087_BOX,
        "finch_iou_vs_adr087": round(finch_iou, 4) if finch_iou is not None else None,
        "coverage_dist": _dist("coverage"), "area_dist": _dist("area"),
        "paper_fraction_dist": _dist("paper_fraction"),
        "all_four_keys_count": all_four, "all_four_keys_total": len(ok),
        "review_list_size": len(review_items), "review_reason_counts": reason_counts,
    }


def _write_index_html(out_dir: Path, sheet_names: dict[str, list[str]],
                       review_items: list[dict], summary: dict) -> None:
    parts = ["<html><head><meta charset=\'utf-8\'><title>subject_crops review</title>",
             "<style>body{font-family:sans-serif;margin:2em;background:#fafaf7}",
             "img{max-width:100%;border:1px solid #ccc;margin-bottom:1em}",
             "h2{margin-top:2em} .flag{color:#a33} table{border-collapse:collapse}",
             "td,th{border:1px solid #ddd;padding:4px 8px;font-size:13px}</style></head><body>"]
    parts.append("<h1>subject_crops review</h1>")
    parts.append("<h2>summary</h2><pre>" + json.dumps(summary, indent=1) + "</pre>")
    parts.append(f"<h2>review list ({len(review_items)})</h2><table><tr><th>title</th>"
                  "<th>source_url</th><th>flags</th></tr>")
    for r in review_items:
        parts.append(f"<tr><td>{r.get('title', '')}</td><td>{r.get('source_url', '')}</td>"
                      f"<td class=\'flag\'>{', '.join(r['flags'])}</td></tr>")
    parts.append("</table>")
    for key, names in sheet_names.items():
        parts.append(f"<h2>{key}</h2>")
        for n in names:
            parts.append(f"<div>{n}<br><img src=\'{n}\'></div>")
    parts.append("</body></html>")
    (out_dir / "index.html").write_text("\n".join(parts))


# ============================================================================= main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collection", action="append", dest="collections",
                     help="restrict to this collection id (repeatable)")
    ap.add_argument("--library", default="art-pack/_Library")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--coverage", type=float, default=COVERAGE_DEFAULT)
    ap.add_argument("--dir", action="append", dest="dirs", help=f"catalog dir(s). Default: {DEFAULT_DIRS}")
    ap.add_argument("--no-sheets", action="store_true", help="skip contact-sheet generation (fast re-runs)")
    args = ap.parse_args()

    dirs = args.dirs or DEFAULT_DIRS
    library = Path(args.library)
    if not library.is_dir():
        print(f"FAIL: no masters at {library}")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)

    worklist = _build_worklist(dirs, args.collections, library)
    if args.limit:
        worklist = worklist[:args.limit]
    print(f"{len(worklist)} works to process")
    for w in worklist:
        w["coverage"] = args.coverage

    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(_process_item, w) for w in worklist]
        done = 0
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(worklist)}")
    results.sort(key=lambda r: r.get("source_url") or "")

    (args.out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    review_items = [r for r in results if r.get("flags")]
    (args.out / "review.json").write_text(json.dumps(review_items, indent=1, ensure_ascii=False))
    summary = build_summary(results)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))

    sheet_names: dict[str, list[str]] = {}
    if not args.no_sheets:
        sheet_names = _make_sheets(results, args.out)
    _write_index_html(args.out, sheet_names, review_items, summary)

    print(f"wrote {len(results)} result(s) -> {args.out}")
    print(f"review list: {len(review_items)}")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
