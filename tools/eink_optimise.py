"""
tools/eink_optimise.py — S5: derive the shipping pre-transform, and measure what it can and cannot buy.
(maintainer tool — NOT part of the runtime image; its OUTPUT ships)

WHAT SHIPS IS A 3-D LUT. `epaper.py` is PIL-only because it runs on the Pi inside the render, so the
production path stays `Color3DLUT` -> Pillow's Floyd-Steinberg (S0.5; measured at 0.034 s/frame). This
module computes that LUT offline. Three stages, all derived, none fitted:

  1. MEDIA-RELATIVE NORMALISATION — the derived white point (S1). Worth 2.48 dE00.
  2. GAMUT CLIP — colorimetric intent (ADR-103). Worth ~0, and that is the finding, not a failure.
  3. QUANTISER PRE-COMPENSATION — the new part, and the only place the remaining error lives.

STAGE 3, AND WHY IT IS NOT A FITTED TONE CURVE. S2 measured that gamma-space error diffusion realises
MORE radiance than its own arithmetic asserts. That is a deterministic, measurable property of the
quantiser: send it a flat patch of colour t and it realises R(t). So send it R^-1(desired) instead.
Nothing is fitted — R is measured by running the production quantiser on flat patches, and the LUT is
its numerical inverse by fixed-point iteration.

⚠️ WHAT THIS CANNOT RECOVER, STATED IN ADVANCE. R is measured on FLAT PATCHES. Real images have local
structure, and error diffusion's behaviour depends on the neighbourhood, so the correction is exact for
smooth regions and approximate everywhere else. The registered prediction before running was that it
would recover 40-70% of the 4.91 dE00 that separates Pillow's FS from a linear-light one.

⚠️ THE FLOOR IS 3.19 dE00 (ADR-103), not zero. An ideal renderer, gamut-clipped and reproduced
continuously with no dithering error at all, still differs from the source by the gamut limit alone.
Any score must be read against that floor and against production's 16.48 — never in isolation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import epaper as ep  # noqa: E402
from tools import eink_color as ec  # noqa: E402
from tools import eink_gamut as eg  # noqa: E402
from tools import eink_panel_model as pm  # noqa: E402

LUT_N = 33
PATCH = 64          # flat-patch size; the realised mean is converged by here (8x8 differs by 0.4%)


def _pal():
    return ep._cached_palette_image("_spectra6_dither", ep.SPECTRA6_DITHER_PALETTE)


def quantise(rgb8) -> np.ndarray:
    """THE PRODUCTION QUANTISER. Everything is optimised with this in the loop, never a model of it."""
    im = Image.fromarray(np.asarray(rgb8, dtype=np.uint8), "RGB")
    return np.asarray(im.quantize(palette=_pal(), dither=Image.Dither.FLOYDSTEINBERG))


def quantiser_response(n: int = LUT_N, patch: int = PATCH) -> np.ndarray:
    """(n,n,n,3) realised LINEAR RGB for a flat patch of each grid colour, through Pillow's FS.

    This is a MEASUREMENT of the shipping quantiser, not a model of it.
    """
    inks = ec.xyz_to_linear_rgb(pm.ink_xyz())
    g = np.linspace(0, 255, n)
    out = np.empty((n, n, n, 3))
    for i, r in enumerate(g):
        for j, gg in enumerate(g):
            for k, b in enumerate(g):
                idx = quantise(np.full((patch, patch, 3), (r, gg, b), dtype=np.uint8))
                out[i, j, k] = inks[idx].reshape(-1, 3).mean(axis=0)
    return out


def _trilinear(grid: np.ndarray, pts_srgb8: np.ndarray) -> np.ndarray:
    """Sample an (n,n,n,3) grid indexed by 0..255 sRGB at arbitrary points."""
    n = grid.shape[0]
    x = np.clip(np.asarray(pts_srgb8, dtype=np.float64), 0, 255) / 255.0 * (n - 1)
    i0 = np.floor(x).astype(int)
    i0 = np.minimum(i0, n - 2)
    f = x - i0
    out = np.zeros(pts_srgb8.shape[:-1] + (3,))
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                w = ((f[..., 0] if dx else 1 - f[..., 0])
                     * (f[..., 1] if dy else 1 - f[..., 1])
                     * (f[..., 2] if dz else 1 - f[..., 2]))
                out += w[..., None] * grid[i0[..., 0] + dx, i0[..., 1] + dy, i0[..., 2] + dz]
    return out


def precompensate(desired_lin: np.ndarray, response: np.ndarray, iters: int = 12) -> np.ndarray:
    """R^-1(desired): the sRGB8 to hand the quantiser so it realises `desired_lin`.

    Fixed-point iteration, which converges quickly because R is close to the identity plus the S2
    error. Nothing here is fitted; it is a numerical inverse of a measured function.
    """
    t_lin = np.clip(desired_lin, 0.0, 1.0)
    for _ in range(iters):
        t8 = np.clip(np.round(ec.linear_to_srgb(np.clip(t_lin, 0, 1)) * 255), 0, 255)
        t_lin = np.clip(t_lin + (desired_lin - _trilinear(response, t8)), 0.0, 1.0)
    return np.clip(np.round(ec.linear_to_srgb(np.clip(t_lin, 0, 1)) * 255), 0, 255).astype(np.uint8)


def build_lut(n: int = LUT_N, response: np.ndarray | None = None,
              precomp: bool = True, knee: float = 1.0) -> np.ndarray:
    """The full source-sRGB -> target-sRGB pre-transform, as an (n,n,n,3) uint8 grid."""
    if response is None and precomp:
        response = quantiser_response(n)
    g = np.linspace(0, 255, n)
    src = np.stack(np.meshgrid(g, g, g, indexing="ij"), axis=-1).reshape(-1, 3)

    mapped = eg.gamut_map(eg.to_media_relative(src), knee=knee)          # stages 1 + 2
    target8 = eg.to_quantiser_srgb8(mapped)
    if not precomp:
        return target8.reshape(n, n, n, 3)
    desired_lin = ec.xyz_to_linear_rgb(ec.lab_to_xyz(mapped, pm.media_white()))
    return precompensate(desired_lin, response).reshape(n, n, n, 3)      # stage 3


def as_pil_lut(grid: np.ndarray) -> ImageFilter.Color3DLUT:
    """Bake to the object `epaper.py` would apply on the Pi. Pillow wants the b axis fastest."""
    n = grid.shape[0]
    table = (np.transpose(grid, (2, 1, 0, 3)).reshape(-1, 3) / 255.0).astype(np.float32)
    return ImageFilter.Color3DLUT(n, table.ravel().tolist(), channels=3)


def apply_lut(rgb8, grid: np.ndarray) -> np.ndarray:
    """Exact per-pixel application (trilinear), for measuring the LUT's own approximation error."""
    return np.clip(np.round(_trilinear(grid.astype(np.float64), np.asarray(rgb8, dtype=np.float64))),
                   0, 255).astype(np.uint8)
