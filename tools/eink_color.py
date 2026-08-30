"""
tools/eink_color.py — the colour-science foundation (maintainer tool, NOT in the runtime image).

WHY THIS EXISTS. Every render decision in this project is currently made on gamma-encoded 8-bit sRGB
with an unweighted RGB metric: `INK_LUM` is a flat channel mean, Pillow's Floyd-Steinberg picks inks by
unweighted squared Euclidean distance, and error is diffused in encoded units. Dither works by
averaging LIGHT, and light is linear in radiance, so none of that arithmetic means what it says. This
module is the smallest correct thing everything else can be rebuilt on.

⚠️ NOTHING HERE IS INVENTED. Every constant is published: the exact sRGB piecewise transfer function
(IEC 61966-2-1 — NOT a 2.2 power, which understates radiance by 19.4x at code 3/255 and 2.9x at 13/255,
converging to 1.0 by midtone; the panel is starved at the dark end and shadow modelling lives in the
RATIOS between dark tones, so that is exactly the wrong place to be wrong), the sRGB/D65 primary matrix, the CIE L*a*b* definition, and
CIEDE2000 (CIE 142-2001). That is the point: after two months of hand-weighted metrics that each
survived only until the next label arrived, the objective's building blocks must be things one can be
WRONG about and be TOLD SO by a published reference.

⚠️ THE WHITE POINT IS ALWAYS EXPLICIT. `xyz_to_lab` takes it as a required argument and has no default.
On reflective media the observer adapts to the brightest neutral in the field, which for this panel is
the WHITE INK (a mediocre grey), not D65 — so "L* 100" means "as light as this panel can be", and a
silent D65 default would quietly answer a different question than the one being asked.

Verified by `tests/test_eink_color.py` against the Sharma/Wu/Dalal (2005) 34-pair CIEDE2000 set, whose
pairs deliberately straddle the hue-quadrant discontinuity and the R_T rotation term — the two places
naive implementations fail silently.
"""
from __future__ import annotations

import numpy as np

# --- sRGB transfer function (IEC 61966-2-1) -------------------------------------------------------
_SRGB_LINEAR_CUT = 0.04045
_SRGB_ENCODED_CUT = 0.0031308

# sRGB (D65) primaries -> CIE XYZ. Standard matrix; rows sum to the D65 white below.
_RGB_TO_XYZ = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
])
_XYZ_TO_RGB = np.linalg.inv(_RGB_TO_XYZ)

#: D65, the sRGB adapting white, Y normalised to 1. Provided for callers that genuinely want absolute
#: colorimetry; for panel work pass the WHITE INK's XYZ instead (see the module docstring).
D65 = _RGB_TO_XYZ.sum(axis=1)

# CIE L*a*b* constants, as exact rationals rather than the rounded 0.008856 / 7.787 often seen.
_LAB_EPS = 216.0 / 24389.0          # (6/29)**3
_LAB_KAPPA = 24389.0 / 27.0         # (29/3)**3


def srgb_to_linear(x):
    """Gamma-encoded sRGB in [0,1] -> linear radiance in [0,1]. Elementwise, any shape."""
    x = np.asarray(x, dtype=np.float64)
    return np.where(x <= _SRGB_LINEAR_CUT, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(y):
    """Linear radiance in [0,1] -> gamma-encoded sRGB in [0,1]. Inverse of `srgb_to_linear`."""
    y = np.asarray(y, dtype=np.float64)
    return np.where(y <= _SRGB_ENCODED_CUT, y * 12.92, 1.055 * np.abs(y) ** (1 / 2.4) - 0.055)


def linear_rgb_to_xyz(rgb):
    """Linear RGB (..., 3) -> CIE XYZ (..., 3)."""
    return np.asarray(rgb, dtype=np.float64) @ _RGB_TO_XYZ.T


def xyz_to_linear_rgb(xyz):
    """CIE XYZ (..., 3) -> linear RGB (..., 3). May fall outside [0,1]: that is out of gamut, not a bug."""
    return np.asarray(xyz, dtype=np.float64) @ _XYZ_TO_RGB.T


def srgb8_to_xyz(rgb8):
    """8-bit gamma-encoded sRGB (..., 3) -> CIE XYZ. The usual entry point for palette values."""
    return linear_rgb_to_xyz(srgb_to_linear(np.asarray(rgb8, dtype=np.float64) / 255.0))


def relative_luminance(rgb8):
    """Y of 8-bit sRGB — real photometric luminance, NOT the flat channel mean.

    The difference is the whole reason this module exists: for this panel's palette the flat mean puts
    white above yellow, while Y puts yellow 38% ABOVE white, because green carries 71.5% of luminance
    and blue only 7.2%.
    """
    return srgb8_to_xyz(rgb8)[..., 1]


def xyz_to_lab(xyz, white):
    """CIE XYZ -> L*a*b*, relative to `white` (an XYZ triple). NO DEFAULT — see the module docstring."""
    xyz = np.asarray(xyz, dtype=np.float64)
    r = xyz / np.asarray(white, dtype=np.float64)
    f = np.where(r > _LAB_EPS, np.cbrt(r), (_LAB_KAPPA * r + 16.0) / 116.0)
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    return np.stack([116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)], axis=-1)


def lab_to_xyz(lab, white):
    """Inverse of `xyz_to_lab`."""
    lab = np.asarray(lab, dtype=np.float64)
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16.0) / 116.0
    fx, fz = fy + a / 500.0, fy - b / 200.0
    def inv(t):
        return np.where(t ** 3 > _LAB_EPS, t ** 3, (116.0 * t - 16.0) / _LAB_KAPPA)
    # Y uses L directly rather than going through f(y): below the linear knee that is the exact
    # inverse, and it avoids a cube-then-uncube round-trip losing precision at the dark end — which is
    # exactly the end of the range this panel's whole problem lives in.
    yr = np.where(L > _LAB_KAPPA * _LAB_EPS, fy ** 3, L / _LAB_KAPPA)
    return np.stack([inv(fx), yr, inv(fz)], axis=-1) * np.asarray(white, dtype=np.float64)


def lab_to_lch(lab):
    """L*a*b* -> L*C*h, hue in degrees [0, 360)."""
    lab = np.asarray(lab, dtype=np.float64)
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    return np.stack([L, np.hypot(a, b), np.degrees(np.arctan2(b, a)) % 360.0], axis=-1)


def lch_to_lab(lch):
    """L*C*h -> L*a*b*."""
    lch = np.asarray(lch, dtype=np.float64)
    L, C, h = lch[..., 0], lch[..., 1], np.radians(lch[..., 2])
    return np.stack([L, C * np.cos(h), C * np.sin(h)], axis=-1)


def ciede2000(lab1, lab2, kL: float = 1.0, kC: float = 1.0, kH: float = 1.0):
    """CIEDE2000 colour difference (CIE 142-2001). Broadcasts over any leading shape.

    ⚠️ THREE BRANCHES ARE WHERE IMPLEMENTATIONS SILENTLY GO WRONG, and each is handled explicitly:
      1. h' is UNDEFINED, not zero, when a' and b' are both zero — and the mean-hue branch below then
         takes the SUM h1'+h2', not the average. Sharma pairs 9-16 exist to catch this.
      2. The mean hue wraps: |h1'-h2'| > 180 needs a 360 correction whose SIGN depends on the sum.
      3. R_T is a ROTATION term, not a scale, and is nonzero only in the blue region around h' 275.
         Dropping it passes most pairs and fails 29-34.
    """
    lab1 = np.asarray(lab1, dtype=np.float64)
    lab2 = np.asarray(lab2, dtype=np.float64)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]

    C1ab, C2ab = np.hypot(a1, b1), np.hypot(a2, b2)
    Cbar_ab = 0.5 * (C1ab + C2ab)
    G = 0.5 * (1.0 - np.sqrt(Cbar_ab ** 7 / (Cbar_ab ** 7 + 25.0 ** 7)))

    a1p, a2p = (1.0 + G) * a1, (1.0 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)

    # Branch 1: h' is undefined when the chroma is exactly zero; the standard sets it to 0 there.
    h1p = np.where((a1p == 0) & (b1 == 0), 0.0, np.degrees(np.arctan2(b1, a1p)) % 360.0)
    h2p = np.where((a2p == 0) & (b2 == 0), 0.0, np.degrees(np.arctan2(b2, a2p)) % 360.0)

    dLp = L2 - L1
    dCp = C2p - C1p
    both = (C1p * C2p) != 0
    dh = h2p - h1p
    dhp = np.where(~both, 0.0,
                   np.where(np.abs(dh) <= 180.0, dh, np.where(dh > 180.0, dh - 360.0, dh + 360.0)))
    dHp = 2.0 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp) / 2.0)

    Lbar = 0.5 * (L1 + L2)
    Cbar = 0.5 * (C1p + C2p)
    hsum = h1p + h2p
    # Branch 2: the mean hue, with its wrap correction — and Branch 1 again, where it is the SUM.
    hbar = np.where(~both, hsum,
                    np.where(np.abs(h1p - h2p) <= 180.0, hsum / 2.0,
                             np.where(hsum < 360.0, (hsum + 360.0) / 2.0, (hsum - 360.0) / 2.0)))

    T = (1.0
         - 0.17 * np.cos(np.radians(hbar - 30.0))
         + 0.24 * np.cos(np.radians(2.0 * hbar))
         + 0.32 * np.cos(np.radians(3.0 * hbar + 6.0))
         - 0.20 * np.cos(np.radians(4.0 * hbar - 63.0)))

    SL = 1.0 + (0.015 * (Lbar - 50.0) ** 2) / np.sqrt(20.0 + (Lbar - 50.0) ** 2)
    SC = 1.0 + 0.045 * Cbar
    SH = 1.0 + 0.015 * Cbar * T

    dtheta = 30.0 * np.exp(-(((hbar - 275.0) / 25.0) ** 2))
    RC = 2.0 * np.sqrt(Cbar ** 7 / (Cbar ** 7 + 25.0 ** 7))
    RT = -np.sin(np.radians(2.0 * dtheta)) * RC          # Branch 3

    tL, tC, tH = dLp / (kL * SL), dCp / (kC * SC), dHp / (kH * SH)
    return np.sqrt(tL ** 2 + tC ** 2 + tH ** 2 + RT * tC * tH)
