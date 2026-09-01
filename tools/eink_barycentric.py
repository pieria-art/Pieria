"""
tools/eink_barycentric.py — dither by decomposing colour into ink WEIGHTS, not by diffusing RGB error.
(maintainer tool — NOT part of the runtime image)

WHY. Floyd-Steinberg carries its error in colour space, and E Ink's own patent (US11721296B2) states
the consequence: *"error diffusion will produce unbounded errors when dithering to colors outside the
convex hull of the primaries."* An RGB error that points OUT of the gamut can never be discharged, so
it accumulates and is dumped on whatever neighbouring pixels can absorb it. Measured on this palette:
a neutral field beside an out-of-gamut region renders from 26% red + 21% blue + 19% green and comes out
(121,121,132) instead of neutral. That is the Sunflowers defect (ADR-106/108) and it has nothing to do
with the white point.

WHAT THIS DOES INSTEAD. Every reproducible colour is a convex combination of the six inks — that is
what "the gamut is their convex hull" means (ADR-099). So: solve for the WEIGHTS directly, then realise
them by choosing one ink per pixel with a noise threshold. The weights are exact, so the local mean is
exact by construction, and nothing leaks sideways.

    target -> weights over 6 inks (sum 1, all >= 0)  ->  per-pixel choice via blue noise

⚠️ IN LINEAR LIGHT, and that is the whole point of ADR-100. Spatial mixing averages RADIANCE, so a
convex combination of inks is only the right model in linear light. Frans-Willem's implementation
decomposes in gamma-encoded sRGB and says so in its TODO — "empirically sRGB is close but not
theoretically justified". We already measured why it is not (ADR-100), so we do not repeat it.

⚠️ CLEAN-ROOM. Barycentric coordinates and convex-hull projection are textbook; nothing here is derived
from `Frans-Willem/epd-dither`'s source, only from the published description of the approach. (That
project is AGPL-3.0-only and Pieria is AGPL-3.0, so licence compatibility is not the constraint — the
reasons not to link are that it is Rust, unpublished, has no gamut stage and carries a diffusion bug.)
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import eink_color as ec  # noqa: E402
from tools import eink_panel_model as pm  # noqa: E402


def ink_vertices() -> np.ndarray:
    """(6, 3) the inks in LINEAR RGB — the space in which spatial mixing is a convex combination."""
    return ec.xyz_to_linear_rgb(pm.ink_xyz())


# --- exact decomposition inside the hull ----------------------------------------------------------
def _tetrahedra(P: np.ndarray):
    """Every non-degenerate 4-subset of the 6 inks, with the 4x4 inverse that gives its barycentrics.

    6 inks and 3 colour dimensions makes the weight problem under-determined (6 unknowns, 4 equations
    counting sum=1). Choosing a TETRAHEDRON makes it exactly determined, which is why the decomposition
    is a search over 4-subsets rather than a least-squares fit — a fit would spread weight over all six
    inks and produce needless speckle.
    """
    out = []
    for tri in itertools.combinations(range(len(P)), 4):
        M = np.vstack([P[list(tri)].T, np.ones(4)])
        if abs(np.linalg.det(M)) < 1e-12:
            continue                                  # coplanar: carries no volume
        out.append((tri, np.linalg.inv(M)))
    return out


def decompose(lin_rgb: np.ndarray, tol: float = -1e-9):
    """(N,3) linear RGB -> (N,6) non-negative weights summing to 1, and an (N,) in-gamut mask.

    Picks, among the tetrahedra containing the point, the one whose LARGEST weight is smallest. That
    favours mixing several inks over leaning on one, which is what makes a dither look smooth rather
    than blotchy; the alternative (maximise the largest weight) gives cleaner flats and coarser
    gradients, and is left as a knob rather than a decision baked in.
    """
    P = ink_vertices()
    pts = np.atleast_2d(np.asarray(lin_rgb, dtype=np.float64))
    hp = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)

    W = np.zeros((len(pts), 6))
    best = np.full(len(pts), np.inf)
    inside = np.zeros(len(pts), dtype=bool)
    for tri, Minv in _tetrahedra(P):
        b = hp @ Minv.T                               # (N,4) barycentric in this tetra
        ok = np.all(b >= tol, axis=1)
        if not ok.any():
            continue
        score = b.max(axis=1)                         # smaller = more evenly mixed
        take = ok & (score < best)
        if take.any():
            best[take] = score[take]
            W[take] = 0.0
            W[np.ix_(take, list(tri))] = b[take]
            inside |= take
    return W, inside


# --- projection to the hull, for everything outside it --------------------------------------------
def _project_to_hull(pts: np.ndarray) -> np.ndarray:
    """Nearest point ON the hull, for points outside it. Exact: faces, then edges, then vertices.

    ⚠️ This is a CLIP, and a clip is not a gamut map. It is here so the decomposition is always
    defined; §3 of the plan (chroma compression) is what stops most pixels ever reaching it.
    """
    P = ink_vertices()
    faces, _, _ = pm.hull_faces()
    best = np.full(len(pts), np.inf)
    out = np.zeros_like(pts)

    def offer(cand):
        nonlocal best, out
        d = ((cand - pts) ** 2).sum(axis=1)
        m = d < best
        best[m], out[m] = d[m], cand[m]

    for i in range(len(P)):                                       # vertices
        offer(np.broadcast_to(P[i], pts.shape).copy())
    for i, j in {tuple(sorted(e)) for f in faces for e in itertools.combinations(f, 2)}:
        a, b = P[i], P[j]                                         # edges
        ab = b - a
        t = np.clip(((pts - a) @ ab) / (ab @ ab), 0.0, 1.0)
        offer(a + t[:, None] * ab)
    for (i, j, k) in faces:                                       # face interiors
        a, b, c = P[i], P[j], P[k]
        n = np.cross(b - a, c - a)
        n = n / np.linalg.norm(n)
        proj = pts - ((pts - a) @ n)[:, None] * n
        M = np.array([[b[0] - a[0], c[0] - a[0]], [b[1] - a[1], c[1] - a[1]], [b[2] - a[2], c[2] - a[2]]])
        uv, *_ = np.linalg.lstsq(M, (proj - a).T, rcond=None)
        u, v = uv
        interior = (u >= 0) & (v >= 0) & (u + v <= 1)
        if interior.any():
            cand = np.where(interior[:, None], proj, np.inf)
            d = ((cand - pts) ** 2).sum(axis=1)
            m = np.isfinite(d) & (d < best)
            best[m], out[m] = d[m], proj[m]
    return out


def decompose_clipped(lin_rgb: np.ndarray):
    """`decompose`, with out-of-gamut input first clipped to the nearest achievable colour."""
    pts = np.atleast_2d(np.asarray(lin_rgb, dtype=np.float64))
    W, inside = decompose(pts)
    if (~inside).any():
        fixed = _project_to_hull(pts[~inside])
        Wf, _ = decompose(fixed)
        W[~inside] = Wf
    return W, inside


# --- realising the weights as one ink per pixel ---------------------------------------------------
def _ign(h: int, w: int) -> np.ndarray:
    """Interleaved gradient noise (Jimenez). Cheap, tiles without seams, decorrelated from the image."""
    y, x = np.mgrid[0:h, 0:w]
    return np.modf(52.9829189 * np.modf(0.06711056 * x + 0.00583715 * y)[0])[0]


def dither(img_srgb8: np.ndarray, noise: np.ndarray | None = None) -> np.ndarray:
    """(H,W,3) uint8 sRGB -> (H,W) ink indices, by weight decomposition + threshold sampling.

    No error is carried between pixels at all, so a saturated out-of-gamut region cannot contaminate
    its neighbours — which is the entire reason this exists.
    """
    a = np.asarray(img_srgb8)
    H, W_ = a.shape[:2]
    lin = ec.srgb_to_linear(a.astype(np.float64) / 255.0).reshape(-1, 3)
    weights, _ = decompose_clipped(lin)

    n = _ign(H, W_).reshape(-1) if noise is None else np.asarray(noise, dtype=np.float64).reshape(-1)
    cdf = np.cumsum(weights, axis=1)
    cdf /= cdf[:, -1:]                                  # guard against float drift off 1.0
    idx = (n[:, None] >= cdf).sum(axis=1)
    return np.clip(idx, 0, 5).astype(np.uint8).reshape(H, W_)
