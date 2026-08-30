"""
tools/eink_dither.py — error diffusion done in LINEAR LIGHT (maintainer tool, NOT in the runtime image).

WHY. Floyd-Steinberg conserves error. What it conserves in this pipeline is error in GAMMA-ENCODED
units, but a fused dither is an average of RADIANCE — so the quantity being conserved is not the
quantity the eye integrates. S1 measured the consequence: the realised image is up to +13.1 L* lighter
in the shadows than the pipeline's own arithmetic believes, exactly where the panel has one ink.

Two independent defects live in the production quantiser, and this module separates them:

    error diffused in gamma-encoded units   ->  the wrong quantity is conserved
    nearest ink by unweighted RGB distance  ->  the wrong ink is chosen

`mode="legacy"` reproduces both (it IS the incumbent, re-implemented). `mode="linear"` fixes both.
Running the same image through each and differencing is how the cost of the incumbent gets a number
instead of an argument.

⚠️ THIS IS A DIAGNOSTIC AND AN UPPER BOUND, NOT A PRODUCTION CHANGE. `epaper.py` is PIL-only because it
runs on the Pi inside the render, and the shipping plan is a 3-D LUT computed offline feeding Pillow's
existing Floyd-Steinberg. Whether the remaining gap after an optimal pre-transform justifies re-opening
that constraint is exactly what this module exists to measure.

THE WAVEFRONT. Floyd-Steinberg looks serial but is not. Pixel (y,x) receives error only from (y,x-1),
(y-1,x-1), (y-1,x) and (y-1,x+1) — every one of which has a strictly smaller k = 2y + x. So all pixels
sharing a k are mutually independent and can be quantised in one vectorised step: 3998 steps for a
1600x1200 frame instead of 1.92 million. Each of the four scatter offsets is injective on its own, so
the buffered `+=` of fancy indexing is safe per offset (it would NOT be safe if the offsets were
combined into one index array — two sources in the same wavefront can hit the same target).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import epaper as ep  # noqa: E402
from tools import eink_color as ec  # noqa: E402
from tools import eink_panel_model as pm  # noqa: E402

# Floyd-Steinberg kernel: (dy, dx, weight). Each offset is injective as a scatter.
_FS = ((0, 1, 7 / 16), (1, -1, 3 / 16), (1, 0, 5 / 16), (1, 1, 1 / 16))
_LUT_N = 64


def _ink_linear() -> np.ndarray:
    return ec.xyz_to_linear_rgb(pm.ink_xyz())


def _ink_srgb8() -> np.ndarray:
    return np.array(ep.SPECTRA6_DITHER_PALETTE, dtype=np.float64)


def nearest_ink_lut(n: int = _LUT_N) -> np.ndarray:
    """(n,n,n) uint8 nearest-ink index over linear RGB in [0,1], by CIEDE2000 in media-relative Lab.

    Precomputed because a per-pixel dE00 inside the diffusion loop is unaffordable; in-loop this is
    three index operations. Grid centres, so the LUT is unbiased rather than favouring the origin.
    """
    g = (np.arange(n) + 0.5) / n
    grid = np.stack(np.meshgrid(g, g, g, indexing="ij"), axis=-1).reshape(-1, 3)
    lab = ec.xyz_to_lab(ec.linear_rgb_to_xyz(grid), pm.media_white())
    inks = ec.xyz_to_lab(pm.ink_xyz(), pm.media_white())
    d = np.stack([ec.ciede2000(lab, np.broadcast_to(ink, lab.shape)) for ink in inks], axis=-1)
    return d.argmin(axis=-1).astype(np.uint8).reshape(n, n, n)


def _lut_lookup(lut: np.ndarray, lin: np.ndarray) -> np.ndarray:
    n = lut.shape[0]
    i = np.clip((lin * n).astype(np.int32), 0, n - 1)
    return lut[i[..., 0], i[..., 1], i[..., 2]]


def dither(img_srgb8: np.ndarray, mode: str = "linear", lut: np.ndarray | None = None) -> np.ndarray:
    """Error-diffuse an (H,W,3) uint8 sRGB image to ink indices (H,W) uint8.

    mode="linear": target and error live in LINEAR RGB; the ink is chosen by dE00. This conserves
                   RADIANCE, which is the quantity a fused dither actually averages.
    mode="legacy": target and error live in gamma-encoded 0..255; the ink is chosen by unweighted
                   squared Euclidean RGB. This is the incumbent, and it is here so the difference
                   between the two is measured by one code path rather than argued between two.
    """
    if mode not in ("linear", "legacy"):
        raise ValueError(f"mode must be 'linear' or 'legacy', not {mode!r}")
    a = np.asarray(img_srgb8)
    H, W = a.shape[:2]

    if mode == "linear":
        target = ec.srgb_to_linear(a.astype(np.float64) / 255.0)
        inks = _ink_linear()
        if lut is None:
            lut = nearest_ink_lut()
    else:
        target = a.astype(np.float64)
        inks = _ink_srgb8()

    out = np.zeros((H, W), dtype=np.uint8)
    lost = np.zeros(3)                      # error that falls off the edge; the conservation test needs it
    ys, xs = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    k = 2 * ys + xs
    order = np.argsort(k.ravel(), kind="stable")
    ky, kx = ys.ravel()[order], xs.ravel()[order]
    bounds = np.searchsorted(k.ravel()[order], np.arange(k.max() + 2))

    for s, e in zip(bounds[:-1], bounds[1:]):
        if s == e:
            continue
        wy, wx = ky[s:e], kx[s:e]
        t = target[wy, wx]
        if mode == "linear":
            idx = _lut_lookup(lut, np.clip(t, 0.0, 1.0))
        else:
            idx = np.argmin(((t[:, None, :] - inks[None, :, :]) ** 2).sum(axis=-1), axis=1)
        out[wy, wx] = idx
        resid = t - inks[idx]
        for dy, dx, w in _FS:
            ny, nx = wy + dy, wx + dx
            ok = (ny < H) & (nx >= 0) & (nx < W)
            if ok.any():
                target[ny[ok], nx[ok]] += w * resid[ok]
            if (~ok).any():
                lost += (w * resid[~ok]).sum(axis=0)
    dither.last_lost = lost                 # inspected by the conservation test
    return out


def realised_linear(idx: np.ndarray) -> np.ndarray:
    """The mean RADIANCE of an ink field — what a viewer at fusing distance integrates."""
    return _ink_linear()[idx].reshape(-1, 3).mean(axis=0)
