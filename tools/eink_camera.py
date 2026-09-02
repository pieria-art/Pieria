"""
tools/eink_camera.py — solve a camera's colour response from a photographed ColorChecker
(maintainer tool — NOT part of the runtime image).

WHY THIS EXISTS. `eink_measure.py` reports colour in CAMERA-NATIVE RGB, corrected only by a per-channel
black/white affine (`solve_correction`). That keeps ratios comparable shot-to-shot, but camera RGB is
not colorimetry: two cameras (or one camera under two illuminants) can report different RGB triples for
the physically identical stimulus. This module is what turns a photograph into a real XYZ/Lab estimate,
by photographing a reference target whose true colorimetry is published — an X-Rite ColorChecker
Classic — and solving for the 3x3 matrix that best explains camera RGB in terms of that truth.

    photograph the 24-patch chart -> mean linear RGB per patch -> solve_camera_matrix -> XYZ/Lab

REFERENCE DATA PROVENANCE. The 24 values below are the CIE L*a*b* (D50, 2° observer) colorimetry of the
X-Rite/GretagMacbeth ColorChecker Classic, in the chart's standard reading order (4 rows x 6 columns,
left-to-right top-to-bottom; patches 19-24 are the achromatic ramp). Sourced from the `colour-science`
Python library's `colour.characterisation` ColorChecker24 dataset (colour-science itself attributes
this to BabelColor/X-Rite measurements), fetched 2026-09-01 since `colour-science` is not a dependency
of this repo (see below) — transcribed here, not imported.

⚠️ X-RITE RECOLOURED THE CHART'S PIGMENTS IN NOVEMBER 2014. A chart bought/printed before that date and
one bought after are measurably different charts, and using the wrong dataset against a real chart is a
SILENT systematic error — every patch is off by roughly 1-3 dE00, in a way that looks exactly like an
ordinary camera-matrix residual rather than a wrong-reference-table bug. `CC24_LAB_AFTER_2014` is the
DEFAULT here because charts sold today are post-2014. `CC24_LAB_BEFORE_2014` is provided for older stock.
**The manufacture date is silk-screened on the back of the physical chart — check it before trusting
either table**, and pass the matching constant explicitly if it says otherwise.

`colour-science` is NOT a dependency here and must not become one — it pulls in scipy and imageio, and
this repo is deliberately dependency-light (PIL + numpy only on the Pi side; this maintainer tool adds
only what `eink_raw`/`eink_color` already require). Hardcoding 24 rows of already-published numbers is
far cheaper than a new dependency tree for two lookup tables.

THE SOLVER IS DELIBERATELY A PLAIN LINEAR 3x3, NOT A ROOT-POLYNOMIAL OR HIGHER-ORDER FIT. With only 24
patches to fit against, anything richer than 9 free parameters overfits the chart and stops meaning
anything on colours the chart didn't sample. The 3x3 is also the physically meaningful object: IF a
camera's spectral sensitivities were a linear combination of the CIE colour-matching functions (the
Luther-Ives condition), a 3x3 would be colorimetrically EXACT, and the residual left over after fitting
one is a direct measurement of how far this camera is from that condition. A fancier fit would hide that
number, not improve on what it means.

⚠️ THE CHART RESIDUAL IS A LOWER BOUND ON ERROR, NOT THE ERROR. Two things it cannot see:
  1. Real cameras fail Luther-Ives — their sensitivities are NOT a linear transform of the human
     colour-matching functions — so the "best" 3x3 is a compromise, exact for no colour and least-wrong
     on average over whatever the fitting set happened to contain.
  2. The chart's residual describes colorimetric agreement on PRINTED PIGMENTS. A matrix trained on
     those pigments is only as good on a DIFFERENT set of colorants as those colorants' spectra happen
     to resemble the chart's — and e-ink electrophoretic ink IS spectrally different from printed
     pigment. Two colours can be metamers under one illuminant (same camera RGB, same human-visible
     colour) and diverge under another, or diverge to a camera whose sensitivities are not human-like,
     even when both are called "red" by eye.
  Nothing here corrects for either. A residual of 1.5 dE00 on the chart means "this matrix is good to
  about 1.5 dE00 ON PIGMENTS LIKE THESE" — it is not a warranty on the panel's actual electrophoretic
  inks. Only a spectrophotometer measuring the panel directly removes this source of error; downstream
  code that quotes this residual as its accuracy budget is quoting the wrong thing.

    python -m tools.eink_camera selftest
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.eink_color import ciede2000, lab_to_xyz, xyz_to_lab  # noqa: E402

#: D50, 2° observer, XYZ normalised to Y=100 — the reference illuminant the ColorChecker tables below
#: are defined against. NOT the D65 used elsewhere in `eink_color` for sRGB/panel work: a ColorChecker
#: is conventionally specified under D50 (the ICC/graphic-arts standard illuminant), and mixing white
#: points here would silently rotate every hue by the D50<->D65 chromatic difference.
D50 = np.array([96.422, 100.000, 82.521])

#: Patch names in the chart's standard reading order — 4 rows x 6 columns, left-to-right top-to-bottom.
#: Used only for readable diagnostics (the worst-patch report); the solver itself is order-agnostic
#: as long as `patch_rgb` rows line up with these.
CC24_NAMES = [
    "dark skin", "light skin", "blue sky", "foliage", "blue flower", "bluish green",
    "orange", "purplish blue", "moderate red", "purple", "yellow green", "orange yellow",
    "blue", "green", "red", "yellow", "magenta", "cyan",
    "white 9.5", "neutral 8", "neutral 6.5", "neutral 5", "neutral 3.5", "black 2",
]

#: PRE-November-2014 ColorChecker Classic, CIE L*a*b* under D50/2°. Kept only for charts known (by the
#: date printed on the back) to predate the reformulation — see the module docstring.
CC24_LAB_BEFORE_2014 = np.array([
    [37.986, 13.555, 14.059], [65.711, 18.130, 17.810], [49.927, -4.880, -21.925],
    [43.139, -13.095, 21.905], [55.112, 8.844, -25.399], [70.719, -33.397, -0.199],
    [62.661, 36.067, 57.096], [40.020, 10.410, -45.964], [51.124, 48.239, 16.248],
    [30.325, 22.976, -21.587], [72.532, -23.709, 57.255], [71.941, 19.363, 67.857],
    [28.778, 14.179, -50.297], [55.261, -38.342, 31.370], [42.101, 53.378, 28.190],
    [81.733, 4.039, 79.819], [51.935, 49.986, -14.574], [51.038, -28.631, -28.638],
    [96.539, -0.425, 1.186], [81.257, -0.638, -0.335], [66.766, -0.734, -0.504],
    [50.867, -0.153, -0.270], [35.656, -0.421, -1.231], [20.461, -0.079, -0.973],
])

#: POST-November-2014 ColorChecker Classic (the reformulated chart), CIE L*a*b* under D50/2° — see the
#: module docstring for provenance and why this is the default. ⚠️ CHECK THE MANUFACTURE DATE PRINTED
#: ON THE BACK OF THE PHYSICAL CHART before trusting this over `CC24_LAB_BEFORE_2014`.
CC24_LAB_AFTER_2014 = np.array([
    [37.54, 14.37, 14.92], [64.66, 19.27, 17.50], [49.32, -3.82, -22.54],
    [43.46, -12.74, 22.72], [54.94, 9.61, -24.79], [70.48, -32.26, -0.37],
    [62.73, 35.83, 56.50], [39.43, 10.75, -45.17], [50.57, 48.64, 16.67],
    [30.10, 22.54, -20.87], [71.77, -24.13, 58.19], [71.51, 18.24, 67.37],
    [28.37, 15.42, -49.80], [54.38, -39.72, 32.27], [42.43, 51.05, 28.62],
    [81.80, 2.67, 80.41], [50.63, 51.28, -14.12], [49.57, -29.71, -28.32],
    [95.19, -1.03, 2.93], [81.29, -0.57, 0.44], [66.89, -0.75, -0.06],
    [50.76, -0.13, 0.14], [35.63, -0.46, -0.48], [20.64, 0.07, -0.46],
])

#: Row indices (0-based) of the six achromatic patches — patches 19-24 in 1-based chart numbering.
NEUTRAL_ROWS = list(range(18, 24))


# --- the solver ------------------------------------------------------------------------------------

def solve_camera_matrix(patch_rgb, reference_lab: np.ndarray = CC24_LAB_AFTER_2014
                         ) -> tuple[np.ndarray, dict]:
    """Solve the 3x3 camera-RGB -> XYZ matrix that best explains a photographed ColorChecker.

    `patch_rgb` is (24, 3) float64 LINEAR camera RGB (see `tools.eink_raw.RawFrame.rgb`'s convention —
    scene-linear, NOT gamma-encoded), one row per patch, in `reference_lab`'s row order (`CC24_NAMES`).

    A PLAIN LINEAR 3x3 least-squares fit, not a root-polynomial or higher-order model — see the module
    docstring for why: 24 patches cannot support more than 9 free parameters without overfitting, and
    the 3x3's residual is the physically meaningful diagnostic (how far this camera is from satisfying
    Luther-Ives), which a richer model would only obscure.

    Returns `(M, report)`. `M @ rgb` (or `apply_camera_matrix`) maps camera RGB to XYZ (D50). `report`
    holds per-patch ΔE00 (`report["de00"]`, shape (24,)), `mean`, `median`, `worst`, and `worst_patch`
    (the `CC24_NAMES` entry, or the row index if `reference_lab` has a different row count).

    ⚠️ `report["mean"]`/`["worst"]` are a LOWER BOUND on real-world colour error, not the error itself —
    see the module docstring's uncertainty section before quoting these numbers as an accuracy budget.
    """
    patch_rgb = np.asarray(patch_rgb, dtype=np.float64)
    reference_lab = np.asarray(reference_lab, dtype=np.float64)
    if patch_rgb.shape != reference_lab.shape:
        raise ValueError(
            f"patch_rgb {patch_rgb.shape} must match reference_lab {reference_lab.shape} — "
            f"same patch count, same row order")

    target_xyz = lab_to_xyz(reference_lab, D50)   # (N, 3)

    # Ordinary least squares for M in `target_xyz ≈ patch_rgb @ M.T`, solved as `patch_rgb @ X ≈
    # target_xyz` (X = M.T) so every one of the 24 patches enters as one row of a single, well-posed
    # linear system — np.linalg.lstsq handles the (over-determined, 24 equations x 3 unknowns per
    # output channel) case directly, with no per-channel loop needed.
    X, _, _, _ = np.linalg.lstsq(patch_rgb, target_xyz, rcond=None)
    M = X.T

    predicted_xyz = apply_camera_matrix(patch_rgb, M)
    predicted_lab = xyz_to_lab(predicted_xyz, D50)
    de00 = np.asarray(ciede2000(predicted_lab, reference_lab), dtype=np.float64)

    worst_idx = int(np.argmax(de00))
    worst_patch = CC24_NAMES[worst_idx] if len(CC24_NAMES) == len(de00) else worst_idx
    report = {
        "de00": de00,
        "mean": float(de00.mean()),
        "median": float(np.median(de00)),
        "worst": float(de00[worst_idx]),
        "worst_patch": worst_patch,
    }
    return M, report


def apply_camera_matrix(rgb, M: np.ndarray) -> np.ndarray:
    """Linear camera RGB (..., 3) -> CIE XYZ (..., 3) under D50, via a matrix from `solve_camera_matrix`.

    May land outside the visible gamut or go negative: that is what an out-of-gamut or noisy input
    looks like through a linear map, not a bug in the map itself.
    """
    rgb = np.asarray(rgb, dtype=np.float64)
    return rgb @ np.asarray(M, dtype=np.float64).T


def camera_rgb_to_lab(rgb, M: np.ndarray, white=D50) -> np.ndarray:
    """Convenience: linear camera RGB (..., 3) -> CIE L*a*b*, straight through `apply_camera_matrix`."""
    return xyz_to_lab(apply_camera_matrix(rgb, M), white)


# --- self-test --------------------------------------------------------------------------------------

#: A hand-picked, deliberately-not-diagonal 3x3 "camera" for the round-trip self-test. Not derived from
#: any real sensor — it exists only to give `solve_camera_matrix` a KNOWN answer to recover, with enough
#: cross-talk between channels (off-diagonal terms ~10-20% of the diagonal) to look like a real Bayer
#: sensor's colour filters rather than a trivially-separable toy.
_TRUE_CAMERA_TO_XYZ = np.array([
    [0.62, 0.19, 0.14],
    [0.24, 0.68, 0.09],
    [0.05, 0.11, 0.79],
])


def _synthesise_camera_rgb(reference_lab: np.ndarray, camera_to_xyz: np.ndarray,
                            noise: float = 0.0, gamma: float | None = None,
                            seed: int = 0) -> np.ndarray:
    """Fake a photograph of the chart: invert a KNOWN camera matrix to get plausible linear camera RGB
    for each reference patch, optionally add sensor noise or a per-channel gamma (to synthesise a
    NON-linear "camera" the plain 3x3 model cannot represent exactly).
    """
    xyz = lab_to_xyz(np.asarray(reference_lab, dtype=np.float64), D50)
    rgb = xyz @ np.linalg.inv(camera_to_xyz).T
    if gamma is not None:
        # Apply and undo an arbitrary normalisation so the gamma acts on a sane [0,1]-ish range rather
        # than on raw XYZ-scale numbers (D50 is Y=100-normalised); a linear rescale before/after keeps
        # this a fair "same camera, non-linear response" comparison rather than also changing exposure.
        lo, hi = rgb.min(), rgb.max()
        span = max(hi - lo, 1e-9)
        unit = np.clip((rgb - lo) / span, 0.0, None)
        rgb = unit ** gamma * span + lo
    if noise:
        rng = np.random.default_rng(seed)
        rgb = rgb + rng.normal(0.0, noise, rgb.shape)
    return rgb


def cmd_selftest(args) -> None:
    print("SELF-TEST — synthetic ColorChecker photographs with a KNOWN camera, check recovery\n")
    ok = True

    # --- structural checks on the hardcoded reference data itself ----------------------------------
    # A transcription typo in 72 hand-entered numbers is the likeliest bug in this whole module, and
    # would otherwise be invisible: nothing downstream would look "wrong", it would just quietly fit a
    # slightly different chart than the one photographed.
    for name, table in (("AFTER_2014", CC24_LAB_AFTER_2014), ("BEFORE_2014", CC24_LAB_BEFORE_2014)):
        L = table[:, 0]
        case_ok = bool(np.all((L >= 0.0) & (L <= 100.0)))
        ok &= case_ok
        print(f"  [{name}] every L* in [0,100]: {'OK' if case_ok else 'FAILED'}")

        case_ok = len({tuple(row) for row in table.tolist()}) == table.shape[0]
        ok &= case_ok
        print(f"  [{name}] all 24 patches distinct: {'OK' if case_ok else 'FAILED'}")

        neutrals = table[NEUTRAL_ROWS]
        case_ok = bool(np.all(np.abs(neutrals[:, 1:]) < 3.0))
        ok &= case_ok
        print(f"  [{name}] neutrals near-neutral (|a*|,|b*| < 3): {'OK' if case_ok else 'FAILED'}")

        neutral_L = neutrals[:, 0]
        case_ok = bool(np.all(np.diff(neutral_L) < 0.0))
        ok &= case_ok
        print(f"  [{name}] neutrals strictly monotonic (white->black): "
              f"{'OK' if case_ok else 'FAILED'}")

        red_a = table[14, 1]     # patch 15 "Red" (1-based) / row 14 (0-based)
        blue_b = table[12, 2]    # patch 13 "Blue" (1-based) / row 12 (0-based)
        case_ok = red_a > 0.0 and blue_b < 0.0
        ok &= case_ok
        print(f"  [{name}] Red has a*>0, Blue has b*<0: {'OK' if case_ok else 'FAILED'} "
              f"(red a*={red_a:.2f}, blue b*={blue_b:.2f})")

    # --- round-trip: exact linear camera, recover the known matrix ---------------------------------
    rgb = _synthesise_camera_rgb(CC24_LAB_AFTER_2014, _TRUE_CAMERA_TO_XYZ)
    M, report = solve_camera_matrix(rgb, CC24_LAB_AFTER_2014)
    matrix_err = float(np.abs(M - _TRUE_CAMERA_TO_XYZ).max())
    case_ok = report["mean"] < 0.01 and matrix_err < 1e-6
    ok &= case_ok
    print(f"\n  round-trip (exact linear camera): mean dE00={report['mean']:.2e} "
          f"max |M - true|={matrix_err:.2e}  {'OK' if case_ok else 'FAILED'}")

    # --- noise robustness: residual rises but stays small, matrix stays close ----------------------
    rgb_noisy = _synthesise_camera_rgb(CC24_LAB_AFTER_2014, _TRUE_CAMERA_TO_XYZ, noise=0.002, seed=1)
    M_noisy, report_noisy = solve_camera_matrix(rgb_noisy, CC24_LAB_AFTER_2014)
    matrix_err_noisy = float(np.abs(M_noisy - _TRUE_CAMERA_TO_XYZ).max())
    case_ok = (report_noisy["mean"] > report["mean"] and report_noisy["mean"] < 2.0
               and matrix_err_noisy < 0.1)
    ok &= case_ok
    print(f"  noise robustness: mean dE00={report_noisy['mean']:.4f} "
          f"(vs {report['mean']:.2e} clean)  max |M - true|={matrix_err_noisy:.2e}  "
          f"{'OK' if case_ok else 'FAILED'}")

    # --- non-linear camera: the 3x3 CANNOT fit it exactly, and must show a visibly worse residual --
    # This is the check that proves the residual is diagnostic rather than a rubber stamp: a metric
    # that reports "good" no matter what is a check that can only pass.
    rgb_gamma = _synthesise_camera_rgb(CC24_LAB_AFTER_2014, _TRUE_CAMERA_TO_XYZ, gamma=1.8)
    _, report_gamma = solve_camera_matrix(rgb_gamma, CC24_LAB_AFTER_2014)
    case_ok = report_gamma["mean"] > 20.0 * max(report["mean"], 1e-9) and report_gamma["mean"] > 1.0
    ok &= case_ok
    print(f"  non-linear camera (per-channel gamma 1.8): mean dE00={report_gamma['mean']:.2f} "
          f"(clean linear case was {report['mean']:.2e})  {'OK' if case_ok else 'FAILED'} — "
          f"a plain 3x3 must NOT be able to absorb this")

    print("\nself-test PASSED" if ok else "\nself-test FAILED")
    if not ok:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="validate the reference data and solver against synthetic cameras")
    args = ap.parse_args()
    {"selftest": cmd_selftest}[args.cmd](args)


if __name__ == "__main__":
    main()
