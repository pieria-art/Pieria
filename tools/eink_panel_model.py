"""
tools/eink_panel_model.py — the panel's own geometry, computed in a real colour space.
(maintainer tool — NOT part of the runtime image)

WHY. Every structural claim this project has made about the panel was computed with `INK_LUM`, a flat
mean of gamma-encoded channels. That is not luminance, and at the top of the range it is not even in
the right ORDER: the flat mean ranks white above yellow, while real luminance ranks yellow 38% above
white, because green carries 71.5% of luminance and blue 7.2%. So the claims have to be re-derived
before anything is built on them.

WHAT IT COMPUTES — all of it arithmetic, no camera, no rig, no labels:
  · the six inks in XYZ / Lab / LCh, absolute and media-relative
  · the ACHIEVABLE GAMUT as the convex hull of the inks IN LINEAR LIGHT. At a viewing distance where
    the dither fuses, error diffusion realises area-weighted averages; averages of RADIANCE are convex
    combinations; so the reproducible set is the hull, a solid, not six points. This is why measuring
    "which ink did this pixel get" was always going to understate the panel.
  · the exact media-relative tone mapping e(d), and what it says about the white-point lever
  · the tone ladder and where it is actually starved

⚠️ EVERY ABSOLUTE NUMBER HERE INHERITS `SPECTRA6_DITHER_PALETTE`, WHICH IS ANOTHER PHYSICAL PANEL'S
MEASUREMENT (Pimoroni's EL133UF1). This module READS that constant and never rewrites it — the standing
prohibition holds, because no colour reference was ever in frame. `_MEASURED_INK_XYZ` below is the one
place a colorimeter reading would land, so that measurement is a one-constant change and every number
downstream re-derives. Structural conclusions (that the gamut is the hull, that the white point is
derived rather than free, the SIGN of the dither error) are robust to the palette being wrong; the
magnitudes are not.

⚠️ A CORRECTION TO THE DESIGN, FOUND WHILE BUILDING. The cusp is NOT available in closed form. The hull
is a polytope in linear RGB, but Lab is a NONLINEAR function of XYZ, so hull edges are CURVES in Lab
and a constant-hue leaf does not cut them at an algebraic point. The cusp table is therefore a dense
surface sample with a stated, verified resolution error — not an exact intersection. Recorded here
because a "closed form" that is quietly a sample is the kind of claim this project keeps finding.
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import epaper as ep  # noqa: E402
from tools import eink_color as ec  # noqa: E402

INK_NAMES = ("black", "white", "red", "yellow", "blue", "green")
BLACK, WHITE = 0, 1

#: The single hook for a real measurement. Set this to a (6, 3) array of CIE XYZ (Y normalised so the
#: white ink is 1.0) once a ColorChecker or colorimeter exists, and everything below re-derives.
#: Until then it is None and the palette is used. It is deliberately NOT a copy of the palette: a hook
#: pre-filled with the thing it is meant to replace is indistinguishable from having replaced it.
_MEASURED_INK_XYZ = None


def ink_xyz() -> np.ndarray:
    """(6, 3) CIE XYZ for the six inks. The one place the ink colorimetry is decided."""
    if _MEASURED_INK_XYZ is not None:
        return np.asarray(_MEASURED_INK_XYZ, dtype=np.float64)
    return ec.srgb8_to_xyz(np.array(ep.SPECTRA6_DITHER_PALETTE, dtype=np.float64))


def media_white() -> np.ndarray:
    """The adapting white: the panel's own white ink. On reflective media that is what the eye adapts
    to, so this — not D65 — is the reference for every lightness statement about the panel."""
    return ink_xyz()[WHITE]


def ink_table() -> list[dict]:
    xyz = ink_xyz()
    lab_abs = ec.xyz_to_lab(xyz, ec.D65)
    lab_med = ec.xyz_to_lab(xyz, media_white())
    lch_med = ec.lab_to_lch(lab_med)
    flat = np.array(ep.SPECTRA6_DITHER_PALETTE, dtype=float).mean(axis=1)
    rows = []
    for i, name in enumerate(INK_NAMES):
        rows.append({
            "ink": name, "srgb": list(ep.SPECTRA6_DITHER_PALETTE[i]),
            "flat_rgb_mean": round(float(flat[i]), 2),      # the incumbent "luminance", for comparison
            "Y": round(float(xyz[i, 1]), 6),
            "L_abs": round(float(lab_abs[i, 0]), 2),
            "L_media": round(float(lab_med[i, 0]), 2),
            "C_media": round(float(lch_med[i, 1]), 2),
            "h_media": round(float(lch_med[i, 2]), 2),
        })
    return rows


# --- The achievable gamut: the convex hull of the inks in LINEAR light ----------------------------
def hull_faces(pts: np.ndarray | None = None, tol: float = 1e-12):
    """Brute-force convex hull of 6 points: every triple whose plane has all others on one side.

    Six points needs no qhull and no scipy — 20 triples. Returns (faces, normals, offsets) with each
    normal pointing OUTWARD, so `pts @ n <= d` for every hull point.
    """
    P = ec.xyz_to_linear_rgb(ink_xyz()) if pts is None else np.asarray(pts, dtype=np.float64)
    faces, normals, offsets = [], [], []
    for tri in itertools.combinations(range(len(P)), 3):
        a, b, c = P[list(tri)]
        n = np.cross(b - a, c - a)
        nn = np.linalg.norm(n)
        if nn < tol:
            continue                                   # degenerate triple, collinear
        n = n / nn
        d = float(n @ a)
        s = P @ n - d
        rest = np.array([s[i] for i in range(len(P)) if i not in tri])
        if np.all(rest <= tol):
            faces.append(tri); normals.append(n); offsets.append(d)
        elif np.all(rest >= -tol):
            faces.append(tri); normals.append(-n); offsets.append(-d)
    return faces, np.array(normals), np.array(offsets)


def hull_edges(faces) -> set:
    e = set()
    for tri in faces:
        for pair in itertools.combinations(sorted(tri), 2):
            e.add(pair)
    return e


def hull_contains(points, tol: float = 1e-9) -> np.ndarray:
    """Is each linear-RGB point inside the achievable gamut?"""
    _, N, D = hull_faces()
    p = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    return np.all(p @ N.T - D <= tol, axis=1)


def hull_volume() -> float:
    """Signed-tetrahedron sum from an interior origin. Units: fraction of the linear RGB unit cube."""
    P = ec.xyz_to_linear_rgb(ink_xyz())
    faces, N, _ = hull_faces()
    c = P.mean(axis=0)
    vol = 0.0
    for (i, j, k), n in zip(faces, N):
        a, b, d = P[i] - c, P[j] - c, P[k] - c
        v = abs(float(np.dot(a, np.cross(b, d)))) / 6.0
        vol += v
    return vol


def hull_intersect(origin, direction, tol: float = 1e-12):
    """Ray/hull intersection: the largest t >= 0 with origin + t*direction still inside.

    This is the gamut-boundary primitive perceptual-intent mapping needs. With 8 faces it is a handful
    of dot products — no boundary-descriptor lookup table, and therefore no interpolation error.
    """
    _, N, D = hull_faces()
    o = np.asarray(origin, dtype=np.float64)
    v = np.asarray(direction, dtype=np.float64)
    num, den = D - N @ o, N @ v
    t_max = np.inf
    for nu, de in zip(num, den):
        if de > tol:                       # the ray leaves through this face
            t_max = min(t_max, nu / de)
        elif de > -tol and nu < -tol:      # parallel and already outside
            return 0.0, o
    if not np.isfinite(t_max):
        return np.inf, o
    return float(max(0.0, t_max)), o + max(0.0, t_max) * v


def gamut_surface_lab(n: int = 160) -> np.ndarray:
    """Dense barycentric sample of every hull face, in MEDIA-RELATIVE Lab.

    Sampled, not solved: see the module docstring. `n` is the subdivision per face edge, so each face
    contributes n(n+1)/2 points. n=160 is where it CONVERGES: measured against an independent
    Monte-Carlo the worst chroma undershoot is 0.60 C* at n=96 and 0.20 at n=160, unchanged at n=256 —
    so 0.20 is the Monte-Carlo's own noise floor, not remaining sampling error.
    """
    P = ec.xyz_to_linear_rgb(ink_xyz())
    faces, _, _ = hull_faces()
    w = []
    for i in range(n + 1):
        for j in range(n + 1 - i):
            w.append((i / n, j / n, (n - i - j) / n))
    W = np.array(w)
    pts = np.concatenate([W @ P[list(tri)] for tri in faces], axis=0)
    return ec.xyz_to_lab(ec.linear_rgb_to_xyz(pts), media_white())


def cusp_table(bins: int = 72, n: int = 160) -> list[dict]:
    """Per-hue maximum chroma on the gamut boundary, and the L* at which it occurs."""
    lab = gamut_surface_lab(n)
    lch = ec.lab_to_lch(lab)
    L, C, h = lch[..., 0], lch[..., 1], lch[..., 2]
    idx = np.minimum((h / 360.0 * bins).astype(int), bins - 1)
    out = []
    for b in range(bins):
        m = idx == b
        if not m.any():
            continue
        k = np.argmax(C[m])
        out.append({"hue_deg": round(b * 360.0 / bins, 1),
                    "C_max": round(float(C[m][k]), 2),
                    "L_at_cusp": round(float(L[m][k]), 2)})
    return out


# --- The tone mapping the physics actually dictates -----------------------------------------------
def media_relative_lut() -> np.ndarray:
    """The exact media-relative tone transform, as an encoded 0..255 LUT.

    A reference is authored against a white of 1.0; the panel's white reflects Y_white of that. Relative
    colorimetric reproduction therefore scales RADIANCE by Y_white and re-encodes:

        e(d) = 255 * srgb_encode( srgb_decode(d/255) * Y_white )

    🔑 THIS IS THE WHITE-POINT LEVER, DERIVED. It is not a preference and not a free knob: it is what
    "put the reference on this paper" means. And it is a CURVE — the ratio e(d)/d runs ~0.33 at the
    dark end to srgb_encode(Y_white) at the top — so the shipped LINEAR scale is the wrong shape as
    well as (probably) the wrong value.

    ⚠️ THE TOP-END RATIO IS srgb_encode(Y_white) = 0.641, NOT Y_white**(1/2.4) = 0.660. An earlier
    statement of this dropped the sRGB affine terms (encode(y) = 1.055*y**(1/2.4) - 0.055) and was
    caught by `test_the_white_point_is_a_curve_not_a_scale`. It happens to land on 163.3/255 = 0.6405,
    the naive palette ratio — but only because THIS white ink is near-neutral, so its flat channel mean
    approximates its encoded luminance. That is a coincidence of neutrality, not an identity, and it
    would not survive a measured ink with any colour cast.
    """
    Yw = float(media_white()[1])
    d = np.arange(256) / 255.0
    return np.round(255.0 * ec.linear_to_srgb(ec.srgb_to_linear(d) * Yw)).astype(int)


def white_point_report() -> dict:
    Yw = float(media_white()[1])
    lut = media_relative_lut()
    d = np.arange(1, 256)
    ratio = lut[1:] / d
    return {
        "Y_white_ink": round(Yw, 6),
        "asymptotic_encoded_ratio": round(float(ec.linear_to_srgb(Yw)), 4),
        "wrong_but_tempting_pow_only": round(Yw ** (1 / 2.4), 4),   # drops the sRGB affine terms
        "naive_palette_ratio": round(float(np.mean(ep.SPECTRA6_DITHER_PALETTE[WHITE]) / 255.0), 4),
        "shipped_constant": ep.SPECTRA6_WHITE_POINT,
        "ratio_at_d": {int(k): round(float(lut[k] / k), 4) for k in (8, 32, 64, 128, 192, 255)},
        # from d=8: below that the LUT's integer rounding dominates and the ratio is quantisation
        # noise, not a statement about the transform.
        "ratio_min_from_d8": round(float((lut[8:] / np.arange(8, 256)).min()), 4),
        "ratio_max": round(float(ratio.max()), 4),
    }


def starvation_report() -> dict:
    """ADR-094's two-ended starvation claim, recomputed in each space. The orders disagree."""
    xyz = ink_xyz()
    spaces = {
        "flat_rgb_mean": np.array(ep.SPECTRA6_DITHER_PALETTE, dtype=float).mean(axis=1),
        "linear_Y": xyz[:, 1],
        "L_media": ec.xyz_to_lab(xyz, media_white())[:, 0],
    }
    out = {}
    for name, v in spaces.items():
        order = np.argsort(v)
        lo, hi = float(v[order[0]]), float(v[order[-1]])
        span = hi - lo
        gaps = [{"from": INK_NAMES[order[i]], "to": INK_NAMES[order[i + 1]],
                 "gap": round(float(v[order[i + 1]] - v[order[i]]), 4),
                 "pct_of_range": round(100.0 * float(v[order[i + 1]] - v[order[i]]) / span, 1)}
                for i in range(len(order) - 1)]
        out[name] = {"order": [INK_NAMES[i] for i in order],
                     "top_ink": INK_NAMES[order[-1]], "gaps": gaps,
                     "largest_gap": max(gaps, key=lambda g: g["gap"])}
    return out


# --- Quantifying the two defects, separately ------------------------------------------------------
# ⚠️ PREDICTION REGISTERED BEFORE RUNNING (ADR-096's guard). The renderer diffuses Floyd-Steinberg error
# in GAMMA-ENCODED units, but the fused image is an average of RADIANCE. sRGB encoding is CONCAVE, so
# by Jensen's inequality the mean of the encodings exceeds the encoding of the mean: the realised
# radiance must therefore come out HIGHER than the pipeline's own arithmetic believes — POSITIVE
# everywhere, never negative. It must be largest where the EOTF's curvature is largest (the shadows),
# and must vanish at both endpoints, where a flat patch lands on a single ink and there is no mixture
# to average. A different SHAPE refutes the theory; it does not adjust it.
def media_ceiling_level() -> float:
    """The source level whose radiance equals the paper's. Above it the reference is simply brighter
    than the panel can be, and no tone curve changes that — it is a gamut fact, not a bug."""
    return 255.0 * float(ec.linear_to_srgb(media_white()[1]))


def dither_error_report(levels=range(0, 256, 8)) -> dict:
    """Defect 1, isolated: what gamma-space error diffusion realises vs what it believes it realised.

    `believed` is the level's own radiance — Floyd-Steinberg conserves the ENCODED value, so that is
    what the pipeline's arithmetic asserts it put on the paper. `realised` is the L* of the actual mean
    RADIANCE of the inks it laid down, which is what a viewer at fusing distance integrates.

    ⚠️ THE FIRST VERSION OF THIS CONFLATED TWO EFFECTS AND THE REGISTERED PREDICTION APPEARED TO FAIL
    (min -42.4 L*). Every negative value was above d = 163.4 — the level whose radiance equals the
    paper's — where the reference is brighter than the panel can be. That is CEILING CLIPPING, a gamut
    fact, and it is reported separately below. Within the achievable range the prediction holds exactly:
    positive everywhere, peaking in the shadows, vanishing at d = 0. Recorded rather than quietly fixed,
    because "the prediction failed, so I changed the measurement" is the move this project guards against
    — what changed is that the measurement was of two things at once.
    """
    from PIL import Image

    inks_lin = ec.xyz_to_linear_rgb(ink_xyz())
    Yw = media_white()
    ceil_d = media_ceiling_level()
    pal = ep._cached_palette_image("_spectra6_dither", ep.SPECTRA6_DITHER_PALETTE)
    dither, clipping = [], []
    for d in levels:
        img = Image.new("RGB", (128, 128), (int(d), int(d), int(d)))
        idx = np.asarray(img.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG))
        realised_lin = inks_lin[idx].reshape(-1, 3).mean(axis=0)       # average LIGHT, as the eye does
        realised_L = float(ec.xyz_to_lab(ec.linear_rgb_to_xyz(realised_lin), Yw)[0])
        believed_L = float(ec.xyz_to_lab(ec.srgb8_to_xyz([d, d, d]), Yw)[0])
        row = {"d": int(d), "believed_L": round(believed_L, 2), "realised_L": round(realised_L, 2),
               "error_L": round(realised_L - believed_L, 2)}
        (dither if d <= ceil_d else clipping).append(row)
    peak = max(dither, key=lambda r: r["error_L"])
    return {
        "media_ceiling_level": round(ceil_d, 1),
        "dither_error_below_ceiling": dither,
        "ceiling_clipping_above": clipping,
        "prediction_holds": {
            "positive_everywhere_below_ceiling": all(r["error_L"] >= -0.01 for r in dither),
            "zero_at_black": abs(dither[0]["error_L"]) < 0.01,
            "peak_error_L": peak["error_L"], "peak_at_d": peak["d"],
            "peak_is_in_the_shadows": peak["d"] <= 64,
        },
    }
def main() -> None:
    rep = {
        "provenance": {
            "ink_source": "SPECTRA6_DITHER_PALETTE (Pimoroni EL133UF1 — ANOTHER PHYSICAL PANEL)"
                          if _MEASURED_INK_XYZ is None else "_MEASURED_INK_XYZ",
            "adapting_white": "the panel's own white ink (reflective media)",
            "palette": [list(c) for c in ep.SPECTRA6_DITHER_PALETTE],
        },
        "inks": ink_table(),
        "gamut": {"faces": len(hull_faces()[0]), "edges": len(hull_edges(hull_faces()[0])),
                  "euler": 6 - len(hull_edges(hull_faces()[0])) + len(hull_faces()[0]),
                  "volume_frac_of_linear_cube": round(hull_volume(), 6)},
        "white_point": white_point_report(),
        "starvation": starvation_report(),
        "cusps": cusp_table(),
        "dither_error": dither_error_report(),
    }
    print(f"{'ink':8s} {'flatmean':>9s} {'Y':>8s} {'L*abs':>7s} {'L*media':>8s} {'C*':>7s} {'h':>7s}")
    for r in rep["inks"]:
        print(f"{r['ink']:8s} {r['flat_rgb_mean']:9.1f} {r['Y']:8.4f} {r['L_abs']:7.1f} "
              f"{r['L_media']:8.1f} {r['C_media']:7.1f} {r['h_media']:7.1f}")
    for space, s in rep["starvation"].items():
        print(f"\n{space}\n  order: {' < '.join(s['order'])}   TOP = {s['top_ink']}")
        print(f"  largest gap: {s['largest_gap']['from']} -> {s['largest_gap']['to']} "
              f"= {s['largest_gap']['pct_of_range']}% of the range")
    wp = rep["white_point"]
    print(f"\nwhite point:  Y_white {wp['Y_white_ink']}  ->  asymptotic ratio "
          f"{wp['asymptotic_encoded_ratio']}   (shipped {wp['shipped_constant']})")
    print(f"  the ratio is a CURVE, not a scale: {wp['ratio_min_from_d8']} .. {wp['ratio_max']}")
    de = rep["dither_error"]
    ph = de["prediction_holds"]
    print(f"\ngamma-space dither error (below the media ceiling d={de['media_ceiling_level']}):")
    print(f"  peak {ph['peak_error_L']:+.1f} L* at d={ph['peak_at_d']}  ·  positive everywhere: "
          f"{ph['positive_everywhere_below_ceiling']}  ·  zero at black: {ph['zero_at_black']}  ·  "
          f"peak in the shadows: {ph['peak_is_in_the_shadows']}")
    print(f"  ABOVE the ceiling, {len(de['ceiling_clipping_above'])} levels clip to the paper white — "
          f"a gamut fact, reported separately")
    print(f"\ngamut: {rep['gamut']['faces']} faces, Euler {rep['gamut']['euler']}, "
          f"{rep['gamut']['volume_frac_of_linear_cube'] * 100:.2f}% of the linear cube")
    dest = ROOT / "bench-eink/analysis/S1_panel_geometry.json"
    dest.write_text(json.dumps(rep, indent=1))
    print(f"\n-> {dest}")


if __name__ == "__main__":
    main()
