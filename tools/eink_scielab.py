"""
tools/eink_scielab.py — S-CIELAB: a perceptual difference that knows about viewing distance.
(maintainer tool — NOT part of the runtime image)

WHY. Every objective this project has tried counted pixels one at a time. None of them modelled the
one fact that makes dithering work at all: at viewing distance the ink pattern FUSES, and what the eye
integrates is a spatially low-passed version of it. A per-pixel metric on a dithered image is measuring
mostly the dither, which is why "grain" kept having to be bolted on as a separate hand-weighted term —
it was the missing spatial model showing through as a residual.

S-CIELAB (Zhang & Wandell 1996) is the standard answer and it is not ours: decompose into opponent
channels, filter each by that channel's contrast sensitivity at the actual viewing geometry, then take
CIEDE2000. The grain term stops being a term. It becomes a consequence of the filter.

⚠️ FILTERED IN THE FOURIER DOMAIN WITH THE ANALYTIC TRANSFER FUNCTION, NOT BY CONVOLUTION. A
unit-volume Gaussian E(r) = (1/pi.sigma^2) exp(-r^2/sigma^2) has the exact transform exp(-pi^2 sigma^2 f^2),
so each channel is ONE analytic multiplier. This is not an optimisation, it is a correctness choice:
  · exact — no kernel sampling, which is what breaks at sigma = 1.46 px (the narrowest kernel here);
  · DC gain is exactly 1 by construction, so "S-CIELAB reduces to CIELAB on a flat patch" holds to
    machine precision rather than approximately;
  · spatial convolution is not merely slower but infeasible — the luminance channel's widest lobe needs
    a 4030 px half-width at 3 m, wider than the image.

⚠️ THE PUBLISHED WEIGHTS DO NOT SUM TO 1 (0.918 / 0.861 / 0.859). They MUST be renormalised or the DC
gain is wrong and every flat region acquires a difference that is not there.

⚠️ ONE DEGENERACY, AND IT IS NOT THE ONE YOU EXPECT. Nothing goes sub-pixel: px/degree INCREASES with
distance, so kernels get WIDER in pixels as the viewer backs away. What does break is the luminance
channel's wide negative lobe, sigma3 = 4.336 deg, against a panel subtending 5.2 x 3.9 deg at 3 m — at
>= 2 m that term is an image-wide DC subtraction whose value is set entirely by the boundary condition.
`w3_zero=True` re-runs without it; if a ranking flips between the two, the ranking is a padding
artefact and not a finding. That check can fail, which is the point of it.

GEOMETRY. Panel active area 270.4 x 202.8 mm at 1600 x 1200 => pitch 0.1690 mm.
⚠️ The parts list's "200 ppi" is WRONG and inconsistent with its own active-area figure; the true
figure is 150.3 ppi. A 33% error in pitch is a 33% error in every angular quantity below.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import eink_color as ec  # noqa: E402
from tools import eink_panel_model as pm  # noqa: E402

PITCH_MM = 270.4 / 1600.0                      # 0.1690 mm — measured, not the vendor's 200 ppi claim
DISTANCES_M = (0.5, 1.0, 1.5, 2.0, 3.0)

# Zhang & Wandell (1996), after Poirson & Wandell. XYZ -> opponent (luminance, red-green, blue-yellow).
_XYZ_TO_OPP = np.array([[0.279, 0.720, -0.107],
                        [-0.449, 0.290, -0.077],
                        [0.086, -0.590, 0.501]])
# The exact inverse, not the published rounded one — they differ by ~0.4% and the exact one is free.
_OPP_TO_XYZ = np.linalg.inv(_XYZ_TO_OPP)

#: (weight, sigma in DEGREES of visual angle) per opponent channel. Weights are renormalised at use.
_CSF = (
    ((0.921, 0.0283), (0.105, 0.133), (-0.108, 4.336)),   # luminance
    ((0.531, 0.0392), (0.330, 0.494)),                    # red-green
    ((0.488, 0.0536), (0.371, 0.386)),                    # blue-yellow
)


def pixels_per_degree(distance_m: float, pitch_mm: float = PITCH_MM) -> float:
    """One pixel subtends 2*atan(pitch / 2d); ppd is its reciprocal in degrees."""
    return 1.0 / np.degrees(2.0 * np.arctan(pitch_mm / 1000.0 / (2.0 * distance_m)))


def _transfer(shape, ppd: float, channel: int, w3_zero: bool = False) -> np.ndarray:
    """H(f) for one opponent channel on an (H, W) grid, f in cycles/degree. DC gain exactly 1."""
    H, W = shape
    fy = np.fft.fftfreq(H)[:, None] * ppd            # cycles/px * px/deg = cycles/deg
    fx = np.fft.rfftfreq(W)[None, :] * ppd
    f2 = fy ** 2 + fx ** 2
    terms = _CSF[channel]
    if w3_zero and channel == 0:
        terms = terms[:2]
    wsum = sum(w for w, _ in terms)
    return sum(w * np.exp(-(np.pi ** 2) * (s ** 2) * f2) for w, s in terms) / wsum


def filter_opponent(xyz, distance_m: float, w3_zero: bool = False) -> np.ndarray:
    """Spatially filter an (H, W, 3) XYZ image by the human CSF at `distance_m`."""
    a = np.asarray(xyz, dtype=np.float64)
    opp = a @ _XYZ_TO_OPP.T
    ppd = pixels_per_degree(distance_m)
    out = np.empty_like(opp)
    for c in range(3):
        F = np.fft.rfft2(opp[..., c])
        out[..., c] = np.fft.irfft2(F * _transfer(a.shape[:2], ppd, c, w3_zero), s=a.shape[:2])
    return out @ _OPP_TO_XYZ.T


def difference(render_xyz, reference_xyz, distance_m: float, w3_zero: bool = False) -> np.ndarray:
    """Per-pixel S-CIELAB dE00 between a render and its reference, at one viewing distance.

    Both sides are filtered — comparing a filtered render against an unfiltered reference would charge
    the render for the eye's own blur.
    """
    w = pm.media_white()
    a = ec.xyz_to_lab(filter_opponent(render_xyz, distance_m, w3_zero), w)
    b = ec.xyz_to_lab(filter_opponent(reference_xyz, distance_m, w3_zero), w)
    return ec.ciede2000(a, b)


def worst_case(render_xyz, reference_xyz, distances=DISTANCES_M, w3_zero: bool = False,
               stride: int = 1) -> dict:
    """Mean dE00 at each distance, and the worst of them — the objective proper.

    `stride` subsamples the dE field AFTER filtering. That is the principled saving: the objective is a
    spatial mean, and a stratified sample of it is an unbiased estimator with a reportable standard
    error. Downsampling the IMAGE instead would destroy the dither pattern, which is the signal.
    """
    per = {}
    for d in distances:
        de = difference(render_xyz, reference_xyz, d, w3_zero)[::stride, ::stride]
        per[d] = {"mean": float(de.mean()), "p95": float(np.percentile(de, 95)),
                  "sem": float(de.std() / np.sqrt(de.size))}
    worst = max(per, key=lambda d: per[d]["mean"])
    return {"per_distance": per, "worst_distance": worst, "objective": per[worst]["mean"]}


def ink_field_xyz(idx) -> np.ndarray:
    """Ink indices -> the XYZ each pixel actually emits."""
    return pm.ink_xyz()[np.asarray(idx)]
