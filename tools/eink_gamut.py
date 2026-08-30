"""
tools/eink_gamut.py — perceptual-intent gamut mapping onto the panel (maintainer tool, NOT shipped).

🔴 THE FINDING THAT SHAPES THIS FILE: ON THIS PALETTE THERE IS NO LIGHTNESS MAPPING TO DO.

The plan expected a tone curve to fall out of perceptual intent's lightness stage — "the S-curve is
derived, not fitted". It does not, and the reason is exact rather than approximate. Relative
colorimetric reproduction scales RADIANCE by Y_white; expressed in MEDIA-RELATIVE L*, that is the
IDENTITY (verified to 0.000000 across the range). The panel's black ink is (0,0,0) and its white ink is
the media white, so the destination lightness range is [0, 100] — precisely the source's. Ranges that
already match need no compression, and nothing S-shaped emerges from compressing nothing.

📏 SO THE TONE PROBLEM WAS NEVER A RANGE PROBLEM, and two months of curve-fitting were aimed at a
misdiagnosis. What is actually wrong with the shadows is two other things, both measured:
    S2  the quantiser conserves the wrong quantity     -> +13 to +21 L* too light
    S1  one ink below blue, 38.3% of the L* range      -> a LEVEL DENSITY problem
A tone curve cannot fix either. It redistributes content inside a range that already matches; it
cannot conserve radiance and it cannot create levels.

⚠️ AND THE ONE THING THAT WOULD PUT A TOE BACK IS A MEASUREMENT WE DO NOT HAVE. The palette gives black
as a PERFECT (0,0,0). Real e-ink black reflects something; if L*_black > 0 the ranges stop matching,
black-point compensation becomes necessary, and it produces a toe. `black_L` below is that parameter,
0.0 today by assumption and not by measurement. This is the single strongest argument in the whole
programme for buying a ColorChecker.

WHAT IS LEFT, AND IT IS ALMOST EVERYTHING: CHROMA. The achievable gamut is 1.11% of the sRGB cube
(S1), so gamut mapping here is a chroma problem end to end. The method is the cusp-knee compression of
the SGCK family: work inside a constant-hue leaf (hue-preserving by construction), aim at an anchor on
the lightness axis at the DESTINATION cusp's lightness for that hue — which swings from L* 45 to
L* 111 across hues, so a fixed anchor would push yellow-greens at a part of the gamut that is not
there — and compress with a knee, so the inner core is untouched and only the outer part moves.

⚠️ BOUNDARY DISTANCES ARE FOUND BY BISECTION, NOT ALGEBRA. The hull is a polytope in linear RGB but
Lab is nonlinear in XYZ, so a straight ray in Lab is a curve in RGB and there is no closed-form
intersection. Bisection is vectorised over all pixels at once and converges to a stated tolerance.
Same correction as S1's cusp: an algebraic-looking answer that is quietly a sample is the failure
signature this project keeps finding, so it is named.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import eink_color as ec  # noqa: E402
from tools import eink_panel_model as pm  # noqa: E402


def in_destination(lab) -> np.ndarray:
    """Is this Lab colour achievable by the panel (inside the fused ink hull)?"""
    lin = ec.xyz_to_linear_rgb(ec.lab_to_xyz(lab, pm.media_white()))
    return pm.hull_contains(lin).reshape(np.asarray(lab).shape[:-1])


def in_source(lab) -> np.ndarray:
    """Is this Lab colour inside the NORMALISED source gamut?

    ⚠️ `[0, Y_white]`, NOT `[0, 1]`. `to_media_relative` has already scaled the source's radiance by
    Y_white, so the source cube it maps onto is scaled too. Testing the un-normalised cube was a second
    instance of the same omission as the missing normalisation itself, and it had the same symptom:
    for a neutral the source and destination boundaries then differ when they should coincide, so the
    knee compresses the grey axis and white lands at L* 96.1 instead of 100. Both were caught by
    `test_lightness_is_the_identity_on_neutrals`.
    """
    xyz = ec.lab_to_xyz(lab, pm.media_white()) / (pm.media_white() / ec.D65)
    lin = ec.xyz_to_linear_rgb(xyz)
    return np.all((lin >= -1e-9) & (lin <= 1.0 + 1e-9), axis=-1)


def _boundary_distance(anchor, direction, inside_fn, hi=400.0, iters=40) -> np.ndarray:
    """Largest t with anchor + t*direction still inside, by vectorised bisection.

    `direction` is a UNIT vector per point, so t is a distance in Lab units.
    """
    lo = np.zeros(anchor.shape[:-1])
    up = np.full(anchor.shape[:-1], hi)
    for _ in range(iters):
        mid = 0.5 * (lo + up)
        ok = inside_fn(anchor + mid[..., None] * direction)
        lo = np.where(ok, mid, lo)
        up = np.where(ok, up, mid)
    return lo


#: The band the cusp anchor may occupy on the neutral axis. Black ink is L* 0 and white ink is L* 100,
#: and the segment between them is inside the hull by convexity — so [0, 100] is the achievable
#: neutral range and nothing neutral exists above it (yellow is the only ink above media white, at
#: C* 85, emphatically not grey).
#:
#: ⚠️ THE 12 L* MARGIN IS NOT DECORATION — IT IS MEASURED. The apexes are where the bipyramid
#: degenerates to a point, so an anchor there makes every ray through it ill-conditioned. Clamping the
#: yellow cusp (true L* 113) flat onto 100 put the anchor exactly where light-yellow content lives and
#: produced real chroma-ordering INVERSIONS: a dense sweep of 2736 (hue, L*) rays found 7 with a
#: backward step, worst -5.85 C*, all at L* 88-98 and hue 75-85. Measured against the margin:
#:
#:      margin    worst backward step    rays worse than -0.5 C*    mean output C*
#:        0             -5.85                     7                     21.55
#:        5             -0.89                     3                     21.54
#:       12             -0.33                     0                     21.50
#:
#: 12 buys strict ordering for 0.05 C* of mean chroma — 0.2%, and the residual -0.33 C* is a third of
#: the ~1 C* discrimination threshold. Ordering is what perceptual intent promises; 0.2% chroma is not
#: a price worth arguing about.
_ANCHOR_MARGIN = 12.0
_NEUTRAL_MIN, _NEUTRAL_MAX = _ANCHOR_MARGIN, 100.0 - _ANCHOR_MARGIN


def cusp_lightness(hue_deg, table=None) -> np.ndarray:
    """L* of the destination cusp at each hue, interpolated from S1's sampled cusp table.

    Cusp-DIRECTED is the point: this panel's cusp lightness swings from L* 43 to L* 113 across hues,
    so aiming every hue at a fixed anchor would compress toward a place the gamut does not reach.

    ⚠️ CLAMPED TO THE ACHIEVABLE NEUTRAL AXIS, and this is not a detail. Three of 72 hue bins have a
    cusp above L* 100 (up to 113, the yellow region). Their raw cusp lightness is a perfectly real
    point ON the gamut boundary, but the ANCHOR has to be a neutral, and a neutral brighter than the
    white ink does not exist. Left unclamped the anchor falls outside the hull, the boundary bisection
    returns zero, and every colour in those hues collapses onto the anchor — which showed up as 48
    chromatic points with hue errors up to 98 degrees. A cusp-directed method on a gamut whose cusp can
    exceed its own white needs this clamp explicitly.
    """
    tab = table if table is not None else pm.cusp_table()
    h = np.array([r["hue_deg"] for r in tab] + [360.0])
    L = np.array([r["L_at_cusp"] for r in tab] + [tab[0]["L_at_cusp"]])
    return np.clip(np.interp(np.asarray(hue_deg) % 360.0, h, L), _NEUTRAL_MIN, _NEUTRAL_MAX)


def gamut_map(lab_src, knee: float = 0.9, black_L: float = 0.0, cusp_tab=None):
    """Map media-relative Lab into the panel's gamut, preserving hue and ordering.

    knee    fraction of the DESTINATION boundary distance left untouched. 1.0 = pure clipping
            (colorimetric intent); lower values trade in-gamut accuracy for out-of-gamut structure.
    black_L L* of the panel's black ink. 0.0 on the current palette, BY ASSUMPTION — the palette gives
            a perfect black and nothing has measured it. Nonzero puts a toe back (see the module
            docstring); it is exposed so that the day a measurement exists it is one argument.
    """
    lab = np.asarray(lab_src, dtype=np.float64)
    flat = lab.reshape(-1, 3)

    # --- lightness: the identity, unless the black point is not actually black -------------------
    L = flat[:, 0]
    if black_L > 0.0:
        L = black_L + L * (100.0 - black_L) / 100.0     # black-point compensation; this IS the toe

    h = ec.lab_to_lch(np.stack([L, flat[:, 1], flat[:, 2]], axis=-1))[:, 2]

    # --- chroma: cusp-directed knee compression, inside the constant-hue leaf --------------------
    anchor_L = cusp_lightness(h, cusp_tab)
    anchor = np.stack([anchor_L, np.zeros_like(anchor_L), np.zeros_like(anchor_L)], axis=-1)
    vec = np.stack([L - anchor_L, flat[:, 1], flat[:, 2]], axis=-1)
    d = np.linalg.norm(vec, axis=-1)
    unit = np.where(d[:, None] > 1e-9, vec / np.maximum(d, 1e-9)[:, None], np.array([1.0, 0.0, 0.0]))

    d_dst = _boundary_distance(anchor, unit, in_destination)
    d_src = np.maximum(_boundary_distance(anchor, unit, in_source), d)   # the point IS in the source

    core = knee * d_dst
    span = np.maximum(d_src - core, 1e-9)
    d_out = np.where(d <= core, d, core + (d_dst - core) * (d - core) / span)
    d_out = np.minimum(d_out, d_dst)                    # never leave the gamut, whatever rounds say

    out = anchor + d_out[:, None] * unit
    return out.reshape(lab.shape)


def to_media_relative(rgb8) -> np.ndarray:
    """STEP 1 — the exact, derived media-relative normalisation. sRGB -> Lab, white on white.

    🔑 THIS IS THE WHITE POINT, AND IT IS NOT A PARAMETER. Relative-colorimetric reproduction scales
    RADIANCE by Y_white — "put this reference on this paper". After it the source's white sits on the
    panel's white at L* 100 and the neutral axis maps [0,100] -> [0,100] as the identity (S1).

    ⚠️ SKIPPING THIS IS A REAL BUG AND IT WAS MADE HERE. Converting sRGB straight to media-relative Lab
    leaves the source white at L* 145.7, so the neutral axis runs 0..145.7 against a destination of
    0..100 — and the chroma knee then squashes the greys, doing badly and implicitly what this step
    does exactly and explicitly. Caught by `test_lightness_is_the_identity_on_neutrals`, which found
    white rendering at L* 96.1 instead of 100.

    ⚠️ IT IS A CHROMATIC ADAPTATION, NOT JUST A LUMINANCE SCALE. Scaling XYZ by Y_white alone maps the
    source white to the right LIGHTNESS but keeps D65's chromaticity, so it lands at L* 100 with a
    small a*,b* offset — the ink white is a* -0.9, b* -0.9 against D65. "Neutral" then means two
    different axes at once, and the chroma knee shaves ~0.07 L* off greys near white. A von Kries
    scaling onto the media white makes the source white land exactly ON it, so the Lab-neutral axis is
    preserved exactly. The effect is small because this ink is nearly neutral; it is not zero, and
    "nearly" is not a thing to leave in an axis definition.
    """
    w = pm.media_white() / ec.D65
    xyz = ec.srgb8_to_xyz(np.asarray(rgb8, dtype=np.float64)) * w
    return ec.xyz_to_lab(xyz, pm.media_white())


def map_srgb8(rgb8, **kw) -> np.ndarray:
    """8-bit sRGB -> gamut-mapped media-relative Lab. Normalise first, then compress chroma."""
    return gamut_map(to_media_relative(rgb8), **kw)


def to_quantiser_srgb8(lab) -> np.ndarray:
    """Media-relative Lab -> the 8-bit sRGB the QUANTISER compares against.

    ⚠️ NO UN-ADAPTATION. The palette stores each ink as the sRGB encoding of its ABSOLUTE XYZ, so that
    is the space `quantize()` measures distance in. Dividing back out by the adaptation (the obvious
    "return to source space" move, and the one made here first) pushes yellow — which is above media
    white — past linear 1.0, where the clip destroys it. The check that catches it is cheap and exact:
    the six inks must round-trip to `SPECTRA6_DITHER_PALETTE` byte for byte.
    """
    lin = ec.xyz_to_linear_rgb(ec.lab_to_xyz(np.asarray(lab, dtype=np.float64), pm.media_white()))
    return np.clip(np.round(ec.linear_to_srgb(np.clip(lin, 0.0, 1.0)) * 255.0), 0, 255).astype(np.uint8)
