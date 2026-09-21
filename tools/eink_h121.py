"""
tools/eink_h121.py — H-121: realise barycentric ink weights without per-pixel IGN threshold sampling.
(maintainer tool — NOT part of the runtime image)

ADR-120's registered next hypothesis, verbatim: *"realise barycentric weights by diffusing the weight
residual (bounded by construction inside the hull) or by a clustered / lower-frequency mask, so the
exact local mean survives and the grain goes."* The panel judged shipping over `eink_barycentric.dither`
6/6 on grain while calling barycentric's COLOUR truer in 5/6 (ADR-120) — the decomposition was right,
the per-pixel IGN-threshold realisation was what put every pixel's ink choice at the noise frequency.
This module replaces only the realisation. Two arms, built separately because they are different bets:

  C — residual diffusion. `eink_barycentric.decompose_clipped` gives the weight vector per pixel; the
      ROUNDING error (the target minus the one-hot ink actually chosen) is carried to not-yet-visited
      neighbours with the classic Floyd-Steinberg kernel — except the vector being carried lives on the
      6-simplex (sums to 0, each component bounded) instead of in unbounded linear RGB. 🔑 That
      boundedness is *why this is not the thing ADR-108/117 blamed*: linear-light Floyd-Steinberg blew
      up on Café Terrace (a magenta band on the awning, a flat sky) because an RGB error pointing
      outside the achievable gamut has nowhere to discharge and accumulates without bound. A residual
      that is a difference of two points already inside (or projected onto) the hull cannot point
      outside it — there is nowhere unbounded for it to go.
  D — clustered/ordered mask. Same weights, same one-ink-per-pixel threshold-sampling mechanism
      `eink_barycentric.dither` already implements — fed a CLUSTERED-DOT mask (a dot growing from a
      tile's centre outward, tileable, lower spatial frequency) instead of the per-pixel
      interleaved-gradient noise that put the decision energy at the highest possible frequency.

Neither touches `eink_barycentric.decompose_clipped` (the decomposition), `epaper.py`, or
`SPECTRA6_DITHER_PALETTE` — this is a maintainer-plane candidate, exactly like variant B.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import eink_barycentric as eba  # noqa: E402
from tools import eink_color as ec  # noqa: E402

# --- Arm C: weight-residual diffusion ---------------------------------------------------------------

def dither_residual(img_srgb8: np.ndarray, serpentine: bool = True) -> np.ndarray:
    """(H,W,3) uint8 sRGB -> (H,W) ink indices, by weight decomposition + Floyd-Steinberg-style
    residual diffusion carried ON THE WEIGHT VECTOR, not on RGB.

    Per pixel: `target` = the decomposed weight plus whatever error was carried in (a point that still
    sums to 1, since every carried residual sums to 0). Choose the ink whose coordinate in `target` is
    LARGEST (the simplex vertex closest to it); carry `target - one_hot(choice)` onward with the
    standard 7/16, 3/16, 5/16, 1/16 kernel (mirrored on serpentine rows, the usual FS improvement — it
    is not what is under test here). That residual sums to exactly 0 and is bounded by construction
    (a difference of two points that both sum to 1), so it cannot blow up the way an out-of-gamut RGB
    error can.

    A pure-Python inner loop by necessity — the whole point is a left-to-right (or serpentine)
    DEPENDENCY chain, so it cannot be vectorised the way `decompose_clipped` is. ~6s for a 1600x1200
    frame on this laptop; acceptable for a five-work comparison, not for a video path.
    """
    a = np.asarray(img_srgb8)
    H, W = a.shape[:2]
    lin = ec.srgb_to_linear(a.astype(np.float64) / 255.0).reshape(-1, 3)
    weights, _ = eba.decompose_clipped(lin)
    w = weights.reshape(H, W, 6).tolist()

    idx = [[0] * W for _ in range(H)]
    err = [[0.0] * 6 for _ in range(W)]
    nxt = [[0.0] * 6 for _ in range(W)]
    for y in range(H):
        row = w[y]
        left_to_right = (not serpentine) or (y % 2 == 0)
        xs = range(W) if left_to_right else range(W - 1, -1, -1)
        step = 1 if left_to_right else -1
        for x in xs:
            t = row[x]
            e = err[x]
            target = [t[k] + e[k] for k in range(6)]
            choice = max(range(6), key=lambda k: target[k])
            idx[y][x] = choice
            target[choice] -= 1.0
            fwd = x + step
            if 0 <= fwd < W:
                dst = err[fwd]
                for k in range(6):
                    dst[k] += target[k] * (7.0 / 16.0)
            if y + 1 < H:
                back = x - step
                if 0 <= back < W:
                    dst = nxt[back]
                    for k in range(6):
                        dst[k] += target[k] * (3.0 / 16.0)
                dst = nxt[x]
                for k in range(6):
                    dst[k] += target[k] * (5.0 / 16.0)
                if 0 <= fwd < W:
                    dst = nxt[fwd]
                    for k in range(6):
                        dst[k] += target[k] * (1.0 / 16.0)
            err[x] = [0.0] * 6
        err, nxt = nxt, err
    return np.array(idx, dtype=np.uint8)


# --- Arm D: clustered/ordered mask --------------------------------------------------------------------

def clustered_mask(n: int = 16) -> np.ndarray:
    """(n, n) values in (0, 1), one per rank 0..n*n-1 ordered by distance from the TILE'S OWN CENTRE
    (wrapped, so the tile repeats without a seam) — a dot growing outward from the centre, the classic
    clustered-dot halftone construction, as opposed to the maximally-DISPERSED Bayer matrix (which is
    what per-pixel IGN already approximates — the thing being moved away from). Larger `n` moves the
    decision energy to a lower spatial frequency: bigger dots, fewer of them per unit area."""
    ys, xs = np.mgrid[0:n, 0:n].astype(np.float64)
    c = (n - 1) / 2.0
    dx = np.minimum(np.abs(xs - c), n - np.abs(xs - c))
    dy = np.minimum(np.abs(ys - c), n - np.abs(ys - c))
    d = np.sqrt(dx * dx + dy * dy)
    order = np.argsort(d.ravel(), kind="stable")
    ranks = np.empty(n * n, dtype=np.float64)
    ranks[order] = np.arange(n * n)
    return ((ranks + 0.5) / (n * n)).reshape(n, n)


def _tile_mask(mask: np.ndarray, h: int, w: int) -> np.ndarray:
    n = mask.shape[0]
    reps_y, reps_x = -(-h // n), -(-w // n)
    return np.tile(mask, (reps_y, reps_x))[:h, :w]


def dither_clustered(img_srgb8: np.ndarray, tile: int = 16) -> np.ndarray:
    """(H,W,3) uint8 sRGB -> (H,W) ink indices: the SAME weight decomposition and the SAME one-ink-
    per-pixel threshold-sampling mechanism as `eink_barycentric.dither` — only the noise field changes,
    from per-pixel IGN to a tiled clustered-dot mask."""
    a = np.asarray(img_srgb8)
    H, W = a.shape[:2]
    noise = _tile_mask(clustered_mask(tile), H, W).reshape(-1)
    return eba.dither(a, noise=noise)


# --- The falsifiable claim: local mean ----------------------------------------------------------------

def local_mean_error(img_srgb8: np.ndarray, idx: np.ndarray, block: int = 16) -> float:
    """Mean absolute error, in ink-fraction POINTS (0..100 scale), between the BLOCK-averaged realised
    ink mix and the BLOCK-averaged decomposed weight, over non-overlapping `block` x `block` windows.

    This is H-121's entire claim made numeric: *"the exact local mean survives."* If this number is not
    small, the hypothesis has failed on its own terms regardless of how the render looks.
    """
    a = np.asarray(img_srgb8)
    H, W = a.shape[:2]
    lin = ec.srgb_to_linear(a.astype(np.float64) / 255.0).reshape(-1, 3)
    weights, _ = eba.decompose_clipped(lin)
    weights = weights.reshape(H, W, 6)
    one_hot = np.eye(6, dtype=np.float64)[np.asarray(idx)]

    bh, bw = H - H % block, W - W % block
    if bh == 0 or bw == 0:
        bh, bw, block = H, W, min(H, W)
    wb = weights[:bh, :bw].reshape(bh // block, block, bw // block, block, 6).mean(axis=(1, 3))
    ob = one_hot[:bh, :bw].reshape(bh // block, block, bw // block, block, 6).mean(axis=(1, 3))
    return float(np.abs(wb - ob).mean()) * 100.0


def spectral_low_freq_share(idx: np.ndarray, pitch_mm: float = 0.1690, view_mm: float = 2000.0,
                             cutoff_cpd: float = 10.0):
    """Radially-averaged power spectrum of the REALISED LUMINANCE signal (each pixel's chosen ink's
    XYZ Y, mean-subtracted), converted to cycles/degree at the panel's real pitch (0.1690 mm, 150.3
    ppi) and an assumed viewing distance (2 m, "across a room" — stated, not measured; changes the
    absolute cpd numbers, not which variants separate). Returns (low_freq_fraction, peak_cpd):
    the fraction of total spectral energy at or below `cutoff_cpd`, and the cpd of the spectrum's peak.

    Why this and not raw adjacent-pixel toggling: Floyd-Steinberg's error carry makes the dither
    PATTERN track the image's own low-frequency tonal structure (a near-DC spectral peak — read as
    "tone"), where independent per-pixel threshold sampling pushes essentially all energy to the
    highest representable frequency (nearest Nyquist — read as "grain"), even though both can have a
    similar RAW per-pixel toggle rate. This is the statistic that actually separates the two regimes.
    """
    idx = np.asarray(idx)
    from tools import eink_panel_model as pm
    Y = pm.ink_xyz()[:, 1]
    sig = Y[idx].astype(np.float64)
    sig = sig - sig.mean()
    P = np.abs(np.fft.fftshift(np.fft.fft2(sig))) ** 2
    H, W = idx.shape
    fy = np.fft.fftshift(np.fft.fftfreq(H))          # cycles/pixel
    fx = np.fft.fftshift(np.fft.fftfreq(W))
    FX, FY = np.meshgrid(fx, fy)
    R = np.sqrt(FX ** 2 + FY ** 2)                    # cycles/pixel
    mm_per_deg = view_mm * np.pi / 180.0
    cpd = R / pitch_mm * mm_per_deg
    bins = np.linspace(0, cpd.max(), 60)
    edges = bins[1:]
    idxs = np.digitize(cpd.ravel(), bins)
    prof = np.array([P.ravel()[idxs == i].sum() for i in range(1, len(bins))])
    total = prof.sum()
    low = float(prof[edges <= cutoff_cpd].sum() / total)
    peak = float(edges[int(np.argmax(prof))])
    return low, peak


def decision_frequency(idx: np.ndarray) -> float:
    """Fraction of adjacent pixel pairs (right- and down-neighbours, averaged) whose ink choice
    differs — a cheap proxy for how much of a realisation's decision energy sits at the highest
    spatial frequency (per-pixel independent thresholding, variant B) versus a lower one (C's carried
    residual, D's clustered mask)."""
    idx = np.asarray(idx)
    h_changes = (idx[:, :-1] != idx[:, 1:]).mean()
    v_changes = (idx[:-1, :] != idx[1:, :]).mean()
    return float((h_changes + v_changes) / 2.0)
