"""
tools/eink_shoot.py — one rig session's raws -> linear panel readings, a camera matrix, ink XYZ
(maintainer tool — NOT part of the runtime image).

`eink_measure read` answers "what did the panel do, in the panel's own black-to-white units" — the
right question for A-vs-B comparisons, and the wrong one for colorimetry, because its affine throws the
absolute level away and its output is camera RGB. This module keeps everything LINEAR and ABSOLUTE
(sensor full scale, dark-subtracted, flare-subtracted, flat-divided) and hands the six inks to a camera
matrix solved on a ColorChecker photographed on the same rig. That is the ADR-108 question: what colour
IS each ink, in units a spectrophotometer would recognise.

THE UNIT EVERYTHING LANDS IN. After the flat-field division a frame is in "fraction of sensor full
scale at the flat's mean illumination". The flat is the white ink, so the panel's white reads the same
number everywhere, and a ColorChecker lying ON the panel (frame X2) reads in the same unit — which is
what lets its white patch (a published 88% reflectance factor) put an absolute scale on the white ink.

WHY THE CAMERA MATRIX COMES FROM THE ON-PANEL CHART, NOT THE PER-FRAME ONE. The flat field covers the
panel only. ADR-110's per-frame chart sits on the flock beside the panel, in the lamp's ~2x gradient
with nothing to correct it: its white-to-black ratio read 25.5 where the published value is 27.6, and
that error is spatial, so it leaks into the 3x3 as a wrong residual. The on-panel chart is fully
flat-corrected. The per-frame chart is used for what it is good at — a per-frame exposure/lamp-wander
normalisation, a ratio taken at one spot — and as an independent, gradient-contaminated cross-check on
the matrix.

WHAT THE FIT MEANS. The 3x3 maps lamp-lit camera RGB to the chart's published D50 colorimetry, so ink
XYZ comes out "as if lit by D50" to the extent the chart's pigments and the panel's electrophoretic inks
share metamerism under this lamp — `eink_camera`'s residual caveat, in full, applies.

    python -m tools.eink_shoot primaries bench-eink/analysis/shoot_2026-09-19.json
"""
from __future__ import annotations

import argparse
import json
import sys
from functools import cache
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import epaper as ep  # noqa: E402
from tools import eink_camera as ecam  # noqa: E402
from tools import eink_chart as echart  # noqa: E402
from tools import eink_color as ec  # noqa: E402
from tools import eink_measure as em  # noqa: E402
from tools import eink_raw  # noqa: E402
from tools import eink_target as et  # noqa: E402

INK_NAMES = et.INK_NAMES


class Shoot:
    def __init__(self, manifest_path):
        self.path = Path(manifest_path)
        self.m = json.loads(self.path.read_text())
        self.vault = Path(self.m["vault"])
        self.w, self.h = self.m["target_size"]
        self.trap_rect = tuple(self.m["trap"])

    # --- raw ingest ---------------------------------------------------------------------------------

    def _arw(self, tag: str) -> Path:
        return self.vault / f"{tag}_{self.m['frames'][tag]['dsc']}.ARW"

    @cache
    def frame(self, tag: str) -> eink_raw.RawFrame:
        """Decoded, dark-subtracted per pixel with the frame's nearest lens-cap frame."""
        dark = self.m["frames"][tag]["dark"]
        return eink_raw.decode(self._arw(tag), dark_frame=self._arw(dark) if dark else None)

    def trap(self, tag: str) -> np.ndarray:
        """Veiling-glare pedestal for THIS frame, fraction of full scale, from its own trap aperture."""
        x0, y0, x1, y1 = self.trap_rect
        return self.frame(tag).rgb[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)

    def clean(self, tag: str) -> np.ndarray:
        """Scene-linear, dark- and flare-subtracted, still in the photograph's own pixel grid."""
        return self.frame(tag).rgb - self.trap(tag)

    # --- geometry -----------------------------------------------------------------------------------

    @cache
    def homography(self, tag: str) -> tuple:
        """(src photo fiducials, dst render fiducials) for this frame — one detection per frame."""
        proxy = em._geometry_proxy(self.clean(tag))
        src = em.refine_fiducials(proxy, self.w, self.h, em.find_fiducials(proxy, self.w, self.h))
        dst = [(float(x), float(y)) for x, y in et.fiducial_centres(self.w, self.h)]
        return tuple(src), tuple(dst)

    def rectified(self, tag: str) -> np.ndarray:
        """(h, w, 3) render-grid resample of `clean`, same units."""
        return em.rectify_float(self.clean(tag), self.w, self.h)

    def photo_to_render(self, tag: str, points) -> list:
        """Map photo-grid points onto the render grid through this frame's own homography."""
        src, dst = self.homography(tag)
        a, b, c, d, e, f, g, h = em._perspective_coeffs(list(src), list(dst))
        out = []
        for x, y in points:
            den = g * x + h * y + 1.0
            out.append(((a * x + b * y + c) / den, (d * x + e * y + f) / den))
        return out

    # --- flat field ---------------------------------------------------------------------------------

    @cache
    def flat(self, tag: str) -> np.ndarray:
        """Illumination map from a settled-white frame, NORMALISED to mean 1.0 over the content box, so
        a divided frame stays in fraction-of-full-scale units. Built by `build_flat_field`'s float
        path (trap-cleaned, rectified in float, smoothed).

        ⚠️ SCALAR, not per-channel. Illumination falloff and vignetting are achromatic, but the white
        INK is not: in raw camera units it reads R:G:B ≈ 1 : 2.7 : 1.5, and a per-channel flat divides
        that colour out of every frame it touches. Measured 2026-09-19: the same ColorChecker read
        R:G:B ratios of 1.02 : 0.37 : 0.64 on the panel (per-channel flat) versus on the flock (no
        flat) — the inverse of the white ink's balance, exactly. A per-channel flat is self-consistent
        for on-panel-only work (the matrix absorbs it) but destroys the white ink's own colour, which
        is one of the six things being measured, and silently breaks any comparison with an
        un-flatted reading. One luminance-like field, applied to all three channels, keeps every
        frame in the same raw camera units."""
        arr = self.frame(tag).rgb * 255.0
        field = em.build_flat_field(arr, self.w, self.h, trap=self.trap(tag) * 255.0).mean(axis=2)
        x0, y0, x1, y1 = et.content_box(self.w, self.h)
        return (field / field[y0:y1, x0:x1].mean())[..., None]

    def panel(self, tag: str, flat_tag: str | None = "auto") -> np.ndarray:
        """The frame on the render grid, flat-divided: fraction of full scale at mean illumination."""
        rect = self.rectified(tag)
        if flat_tag == "auto":
            flat_tag = self.m["frames"][tag]["flat"]
        return rect / self.flat(flat_tag) if flat_tag else rect

    # --- readings -----------------------------------------------------------------------------------

    def inks(self, tag: str, inset: float = 0.30, **kw) -> dict:
        """The six large fields of a `primaries` frame: mean and std per ink, linear units."""
        img = self.panel(tag, **kw)
        x0, y0, x1, y1 = et.content_box(self.w, self.h)
        cw, ch = x1 - x0, y1 - y0
        out = {}
        for i, name in enumerate(INK_NAMES):
            cx, cy = i % 3, i // 3
            rx0, ry0 = x0 + cx * cw // 3, y0 + cy * ch // 2
            rx1, ry1 = x0 + (cx + 1) * cw // 3, y0 + (cy + 1) * ch // 2
            dx, dy = int((rx1 - rx0) * inset), int((ry1 - ry0) * inset)
            a = img[ry0 + dy:ry1 - dy, rx0 + dx:rx1 - dx].reshape(-1, 3)
            out[name] = {"mean": a.mean(axis=0), "std": a.std(axis=0), "n": int(a.shape[0])}
        return out

    def chart_flock(self, tag: str) -> dict:
        """The per-frame chart on the flock: NOT flat-corrected (outside the panel), flare-subtracted."""
        return echart.read_chart(self.clean(tag), self.m["chart_flock"], half=self.m["chart_half"])

    def chart_panel(self, tag: str = "X2") -> dict:
        """The on-panel chart, flat-corrected: corners mapped through the frame's homography onto the
        render grid, sampled there. The sampling box scales with the homography (photo px -> render
        px is ~0.65 here), so `half` is rescaled to keep the same physical patch coverage."""
        corners = self.photo_to_render(tag, self.m["chart_panel"])
        src, dst = self.homography(tag)
        scale = np.hypot(dst[1][0] - dst[0][0], dst[1][1] - dst[0][1]) / \
            np.hypot(src[1][0] - src[0][0], src[1][1] - src[0][1])
        half = max(4, int(round(self.m["chart_half"] * scale)))
        return echart.read_chart(self.panel(tag), corners, half=half)


# --- colorimetry ----------------------------------------------------------------------------------

def ink_colorimetry(ink_rgb: np.ndarray, M: np.ndarray) -> dict:
    """Six inks (6,3) linear camera RGB -> XYZ (D50, Y in % reflectance factor), absolute Lab, and
    media-relative Lab (the white INK as the adapting white, ADR-108/eink_color doctrine)."""
    xyz = ecam.apply_camera_matrix(ink_rgb, M)
    white = xyz[INK_NAMES.index("white")]
    lab_abs = ec.xyz_to_lab(xyz, ecam.D50)
    lab_med = ec.xyz_to_lab(xyz, white)
    return {"xyz": xyz, "lab_abs": lab_abs, "lab_media": lab_med,
            "Y_rel_white": xyz[:, 1] / white[1]}


#: Bradford cone response, the ICC chromatic-adaptation transform. The chart's colorimetry is D50;
#: `eink_color`'s sRGB and `eink_panel_model`'s absolute Lab are D65. Two illuminants in one number
#: is exactly the silent hue rotation `eink_camera` warns about, so every D50 result is adapted here.
_BRADFORD = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])
D65_100 = ec.D65 * 100.0


def adapt(xyz, src_white, dst_white) -> np.ndarray:
    """Bradford chromatic adaptation of XYZ (…, 3) from `src_white` to `dst_white` (same Y scale)."""
    src = _BRADFORD @ np.asarray(src_white, dtype=np.float64)
    dst = _BRADFORD @ np.asarray(dst_white, dtype=np.float64)
    T = np.linalg.inv(_BRADFORD) @ np.diag(dst / src) @ _BRADFORD
    return np.asarray(xyz, dtype=np.float64) @ T.T


def palettes(xyz_d50: np.ndarray) -> dict:
    """The six inks as (a) the `eink_panel_model._MEASURED_INK_XYZ` hook value — D65, white ink Y=1 —
    and (b) two sRGB tables: ABSOLUTE (D65-adapted, so the white ink comes out as the grey it is) and
    MEDIA-RELATIVE (the white INK adapted to the sRGB white, so it comes out 255 and every other ink is
    stated relative to it — the chromatic-adaptation model ADR-108 §4 says is the right one for a
    reflective display)."""
    xyz65 = adapt(xyz_d50, ecam.D50, D65_100)
    white = xyz65[INK_NAMES.index("white")]
    hook = xyz65 / white[1]
    def srgb8(xyz_y1):
        return np.clip(np.round(ec.linear_to_srgb(np.clip(ec.xyz_to_linear_rgb(xyz_y1), 0, 1)) * 255), 0, 255)
    absolute = srgb8(xyz65 / 100.0)
    media = srgb8(adapt(xyz65, white, D65_100) / white[1])
    return {"hook_xyz_d65_white1": hook, "srgb_absolute_d65": absolute, "srgb_media_relative": media,
            "clipped_media": [n for n, v in zip(INK_NAMES, ec.xyz_to_linear_rgb(adapt(xyz65, white, D65_100) / white[1]))
                              if (v < 0).any() or (v > 1).any()]}


def _fmt(v, nd=1):
    return "[" + ", ".join(f"{float(x):.{nd}f}" for x in np.atleast_1d(v)) + "]"


def cmd_primaries(args) -> None:
    sh = Shoot(args.manifest)
    out = {"manifest": str(sh.path), "frames": {}}

    # 1. The camera matrix AND the on-panel glare pedestal, from the chart lying on the panel.
    cp = sh.chart_panel(args.chart_frame)
    rep = cp["report"]
    print(f"on-panel chart ({args.chart_frame}): ladder monotonic {rep['ladder_monotonic']}, "
          f"neutral spread {rep['neutral_spread']:.3f}, white/black {rep['ladder_white_to_black']:.1f} "
          f"(published 27.6), worst non-uniformity {rep['worst_nonuniformity']:.3f}")
    if not rep["ok"]:
        raise SystemExit("the on-panel chart failed validation — do not fit a matrix to it")
    chart_pct = cp["rgb"] * 100.0                                       # % of full scale
    M3, fit3 = ecam.solve_camera_matrix(chart_pct)
    M, ped, fit = ecam.solve_camera_affine(chart_pct)
    print(f"  plain 3x3 fit:  mean dE00 {fit3['mean']:.2f}, worst {fit3['worst']:.2f} ({fit3['worst_patch']})")
    print(f"  affine fit:     mean dE00 {fit['mean']:.2f}, worst {fit['worst']:.2f} ({fit['worst_patch']})"
          f"   pedestal {_fmt(ped, 3)} %FS  (trap read {_fmt(sh.trap(args.chart_frame) * 100, 3)})")
    ladder_after = (chart_pct[18] - ped).mean() / (chart_pct[23] - ped).mean()
    print(f"  white/black after the pedestal: {ladder_after:.1f}")

    # 2. The per-frame chart on the flock: same units now (scalar flat), NOT flat-corrected. Its own
    #    affine pedestal must be ~0 (nothing bright beside it) — a check that can fail.
    cf = sh.chart_flock(args.frame)
    chart_f = cf["rgb"] * 100.0
    Mf, fitf = ecam.solve_camera_matrix(chart_f)
    _, pedf, fitfa = ecam.solve_camera_affine(chart_f)
    print(f"on-flock chart ({args.frame}, no flat): white/black {cf['report']['ladder_white_to_black']:.1f}, "
          f"3x3 mean dE00 {fitf['mean']:.2f} worst {fitf['worst']:.2f} ({fitf['worst_patch']}); "
          f"affine pedestal {_fmt(pedf, 3)} %FS (expect ~0), mean dE00 {fitfa['mean']:.2f}")
    bright = [i for i in range(24) if i != 23]
    ratio = (chart_pct[bright] / chart_f[bright])
    print(f"  on-panel / on-flock per-patch ratio (illumination only, if both are clean): "
          f"mean {_fmt(ratio.mean(axis=0), 3)}, spread {_fmt(ratio.std(axis=0) / ratio.mean(axis=0), 3)}")

    # 3. The inks: on the panel, so under the on-panel pedestal.
    inks = sh.inks(args.frame)
    rgb = np.stack([inks[n]["mean"] for n in INK_NAMES]) * 100.0
    std = np.stack([inks[n]["std"] for n in INK_NAMES]) * 100.0
    col = ink_colorimetry(rgb - ped, M)
    col_np = ink_colorimetry(rgb, M3)          # what the 3x3-without-pedestal would have said
    colf = ink_colorimetry(rgb - ped, Mf)      # the flock matrix, same units, same pedestal
    print(f"\ninks from {args.frame} (flat {sh.m['frames'][args.frame]['flat']}, trap "
          f"{_fmt(sh.trap(args.frame) * 100, 3)} %FS, on-panel pedestal {_fmt(ped, 3)} subtracted)")
    print(f"{'ink':7s} {'camera RGB %FS':>24s} {'std':>6s}  {'XYZ (D50, %)':>22s}  "
          f"{'L*a*b* abs':>22s}  {'L*a*b* media':>22s}  Y/Yw")
    for i, n in enumerate(INK_NAMES):
        print(f"{n:7s} {_fmt(rgb[i], 3):>24s} {std[i].mean():6.3f}  {_fmt(col['xyz'][i], 2):>22s}  "
              f"{_fmt(col['lab_abs'][i]):>22s}  {_fmt(col['lab_media'][i]):>22s}  "
              f"{col['Y_rel_white'][i]:.3f}")
    yw = col["Y_rel_white"]
    iy, ib = INK_NAMES.index("yellow"), INK_NAMES.index("black")
    print(f"\nyellow/white Y: {yw[iy] - 1:+.1%}   (vendor swatch +37.6%, Tier-0 glass +6%, "
          f"spectrophotometer on a sibling panel -13.1%)")
    print(f"black/white Y:  {yw[ib]:.1%}   (spectrophotometer 2.5/31 = 8.1%)")
    print(f"white ink Y (absolute, % reflectance factor, as-if-D50): {col['xyz'][1, 1]:.1f}  "
          f"(spectrophotometer ~31)")
    print(f"sensitivity — without the pedestal (plain 3x3): yellow/white {col_np['Y_rel_white'][iy] - 1:+.1%}, "
          f"black/white {col_np['Y_rel_white'][ib]:.1%}; via the on-flock matrix: yellow/white "
          f"{colf['Y_rel_white'][iy] - 1:+.1%}, black/white {colf['Y_rel_white'][ib]:.1%}, "
          f"max |dLab media| {_fmt(np.abs(col['lab_media'] - colf['lab_media']).max(axis=0))}")

    out["camera"] = {"M_affine": M.tolist(), "pedestal_pct_fs": ped.tolist(),
                     "fit_affine": {"mean": fit["mean"], "worst": fit["worst"], "worst_patch": fit["worst_patch"],
                                    "de00": fit["de00"].tolist()},
                     "fit_plain": {"mean": fit3["mean"], "worst": fit3["worst"]},
                     "M_flock": Mf.tolist(), "flock_pedestal_pct_fs": pedf.tolist(),
                     "fit_flock": {"mean": fitf["mean"], "worst": fitf["worst"]},
                     "input_units": "% of sensor full scale, scalar-flat-normalised, dark+trap subtracted"}
    out["frames"][args.frame] = {
        "ink_rgb_pct_fs": rgb.tolist(), "ink_std_pct_fs": std.tolist(),
        "xyz_d50": col["xyz"].tolist(), "lab_abs_d50": col["lab_abs"].tolist(),
        "lab_media": col["lab_media"].tolist(), "Y_rel_white": col["Y_rel_white"].tolist(),
        "lab_media_no_pedestal": col_np["lab_media"].tolist(),
        "lab_media_via_flock_matrix": colf["lab_media"].tolist(),
    }
    # 4. The palette, three ways.
    pal = palettes(col["xyz"])
    print("\nPALETTE — `_MEASURED_INK_XYZ` hook (D65, white ink Y=1):")
    for i, n in enumerate(INK_NAMES):
        print(f"  {n:7s} {_fmt(pal['hook_xyz_d65_white1'][i], 4)}")
    print("sRGB, absolute (D65-adapted; the white ink IS a grey):      "
          + "  ".join(f"{n}:{_fmt(pal['srgb_absolute_d65'][i], 0)}" for i, n in enumerate(INK_NAMES)))
    print("sRGB, media-relative (white ink -> 255; renderer palette):  "
          + "  ".join(f"{n}:{_fmt(pal['srgb_media_relative'][i], 0)}" for i, n in enumerate(INK_NAMES)))
    if pal["clipped_media"]:
        print(f"  ⚠️ outside sRGB before clipping (stated, not hidden): {pal['clipped_media']}")
    print("vendor swatch SPECTRA6_DITHER_PALETTE, for comparison:      "
          + "  ".join(f"{n}:{list(c)}" for n, c in zip(INK_NAMES, ep.SPECTRA6_DITHER_PALETTE)))
    out["palette"] = {k: (v.tolist() if hasattr(v, "tolist") else v) for k, v in pal.items()}

    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1))
        print(f"\n-> {args.out}")


def cmd_bookend(args) -> None:
    """Frame-to-frame repeatability of the six inks, in the units that matter for ADR-110's bar."""
    sh = Shoot(args.manifest)
    cp = sh.chart_panel(args.chart_frame)
    M, ped, _ = ecam.solve_camera_affine(cp["rgb"] * 100.0)
    ref = None
    print(f"{'frame':6s} {'chart white %FS':>22s}  per-ink dE00 vs {args.frames[0]} (media Lab)   "
          f"black/white   yellow/white   worst |d| /255 panel-relative")
    for tag in args.frames:
        d = sh.inks(tag)
        rgb = np.stack([d[n]["mean"] for n in INK_NAMES]) * 100.0
        std = np.stack([d[n]["std"] for n in INK_NAMES]) * 100.0
        cw = sh.chart_flock(tag)["rgb"][18] * 100.0
        col = ink_colorimetry(rgb - ped, M)
        rel = (rgb - rgb[0]) / (rgb[1] - rgb[0]) * 255.0          # this frame's own black=0/white=255
        if ref is None:
            ref = (col["lab_media"], rel)
        de = ec.ciede2000(col["lab_media"], ref[0])
        worst = np.abs(rel - ref[1]).max()
        ghost = std[0].mean() / max(std[1].mean(), 1e-9)
        print(f"{tag:6s} {_fmt(cw, 2):>22s}  {_fmt(de, 2):>36s}   {col['Y_rel_white'][0]:.3f}        "
              f"{col['Y_rel_white'][3] - 1:+.1%}        {worst:5.1f}"
              + (f"   ⚠️ black-field std is {ghost:.0f}x the white's — ghosting" if ghost > 4 else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("primaries", help="the six inks in XYZ/Lab via the on-panel ColorChecker")
    p.add_argument("manifest")
    p.add_argument("--frame", default="F1")
    p.add_argument("--chart-frame", default="X2")
    p.add_argument("--out", default="")
    b = sub.add_parser("bookend", help="ink repeatability across the primaries frames")
    b.add_argument("manifest")
    b.add_argument("--frames", nargs="+", default=["F1", "B1", "B2", "B1x", "B2x"])
    b.add_argument("--chart-frame", default="X2")
    args = ap.parse_args()
    {"primaries": cmd_primaries, "bookend": cmd_bookend}[args.cmd](args)


if __name__ == "__main__":
    main()
