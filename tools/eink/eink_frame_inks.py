"""
tools/eink_frame_inks.py — re-read the 2026-09-19 art frames against the MEASURED inks, per pixel.
(maintainer tool — NOT part of the runtime image)

WHY. ADR-116 measured the six Spectra 6 inks through a ColorChecker on the panel and found the vendor
swatch wrong on every chromatic ink (black L* 39.6 not 0; yellow ~= white in luminance; blue outside
sRGB). ADR-106's "the Sunflowers panel photograph is 43% red + 44% green, 8% yellow" was classified
AGAINST THE SWATCH, while the renderer digitally sends 48% yellow (ADR-117) — arithmetic on bad inputs.
This module is the instrument that re-reads the panel photographs against the measured palette. It does
NOT fix the defect and does NOT touch the shipping renderer (`epaper.py` is untouched, ADR-084/ADR-120).

THE CHAIN — every stage reused, none reinvented:

    raw .ARW --tools.eink.eink_shoot.Shoot--> dark+trap-cleaned, rectified, SCALAR-flat-divided render-grid
    frame (fraction of sensor full scale) --tools.eink.eink_camera.solve_camera_affine (on-panel chart,
    frame X2)--> camera matrix M + veiling-glare pedestal --tools.eink.eink_shoot.adapt (Bradford,
    D50->D65, the exact transform `eink_shoot primaries` uses for its palette hook)--> per-pixel
    absolute XYZ, D65, same units as `eink_panel_model.ink_xyz()` --tools.eink.eink_barycentric.decompose_
    clipped (monkeypatching `eink_panel_model._MEASURED_INK_XYZ` swaps the palette it decomposes
    against)--> non-negative ink weights, vectorised, with the residual retained.

`tools/eink_shoot.py`'s `Shoot` class already does raw ingest, dark/trap, homography, and the scalar
flat division; its `chart_panel()`/`ink_colorimetry()`/`adapt()` already solve the camera matrix and do
the D50->D65 Bradford step. This module wires those to a per-PIXEL image instead of six ink-field means,
and adds the one genuinely new piece: per-pixel non-negative unmixing (`eink_barycentric.decompose_
clipped`, which already solves this for images in linear light) plus the residual it deliberately keeps.

    python -m tools.eink.eink_frame_inks report bench-eink/analysis/shoot_2026-09-19.json \
        --out bench-eink/analysis/frame_inks_2026-09-19.json
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.eink import eink_barycentric as eba  # noqa: E402
from tools.eink import eink_camera as ecam  # noqa: E402
from tools.eink import eink_color as ec  # noqa: E402
from tools.eink import eink_panel_model as pm  # noqa: E402
from tools.eink import eink_shoot as esh  # noqa: E402

INK_NAMES = pm.INK_NAMES


# --- palette swap, for the "S" (vendor swatch) readout ---------------------------------------------
@contextlib.contextmanager
def swatch_palette():
    """Temporarily make `eink_panel_model.ink_xyz()` fall back to `SPECTRA6_DITHER_PALETTE`.

    `eink_barycentric.decompose_clipped` decomposes against `pm.ink_xyz()` with no parameter to swap
    it, and it is not cached, so flipping the module's own hook (which `ink_xyz()` already checks —
    "set to None to fall back to the vendor swatch") is the reuse path, not a second decomposer.
    `ec.srgb8_to_xyz` (what `ink_xyz()` calls when the hook is None) returns absolute D65 XYZ in the
    SAME units (Y in 0..1, perfect diffuser = 1) as `_MEASURED_INK_XYZ` — so a pixel's measured XYZ
    can be decomposed against either table without any rescaling.
    """
    saved = pm._MEASURED_INK_XYZ
    pm._MEASURED_INK_XYZ = None
    try:
        yield
    finally:
        pm._MEASURED_INK_XYZ = saved


# --- camera matrix, reused from tools.eink.eink_shoot ----------------------------------------------------
def solve_matrix(sh: esh.Shoot, chart_frame: str = "X2"):
    """The on-panel-chart camera matrix + veiling-glare pedestal, exactly as `eink_shoot primaries`
    computes them (§2 of PRIMARIES_2026-09-19.md). Returns (M, pedestal_pct_fs, fit_report)."""
    cp = sh.chart_panel(chart_frame)
    rep = cp["report"]
    if not rep["ok"]:
        raise SystemExit(f"the on-panel chart ({chart_frame}) failed validation — refusing to fit")
    chart_pct = cp["rgb"] * 100.0
    M, ped, fit = ecam.solve_camera_affine(chart_pct)
    return M, ped, fit


# --- per-pixel colorimetry ---------------------------------------------------------------------------
def frame_xyz_d65(sh: esh.Shoot, tag: str, M: np.ndarray, ped: np.ndarray, *, flat_tag="auto",
                   content_only: bool = True) -> np.ndarray:
    """(H, W, 3) absolute XYZ, D65, in `eink_panel_model.ink_xyz()`'s own units (Y 0..1, white ink
    ~0.376) — for every pixel of frame `tag`, on the render grid.

    Same three steps as `eink_shoot.ink_colorimetry` + `eink_shoot.palettes`' D50->D65 adaptation,
    applied per pixel instead of to six ink-field means: percent-full-scale, minus the SAME on-panel
    pedestal, through the SAME camera matrix, Bradford-adapted D50->D65 with `eink_shoot.adapt` (the
    exact function the primaries path uses — not a hand-rolled matrix), then rescaled from "%
    reflectance factor" to the 0..1 fraction `_MEASURED_INK_XYZ` is stated in.

    `content_only=True` (the default) restricts the output to `eink_target.content_box` — the same
    region `Shoot.inks()` and the digital render's content live in. Without it the black registration
    frame and fiducial squares are part of the array and get unmixed as if they were content, which is
    a measurement of the frame artwork, not of the panel's furniture — caught on V1 (the flat frame
    read only 93.6% white with a 42.7 max residual until this crop was added).
    """
    rgb_pct = sh.panel(tag, flat_tag=flat_tag) * 100.0        # (H, W, 3), % of full scale
    if content_only:
        x0, y0, x1, y1 = _content_box(sh)
        rgb_pct = rgb_pct[y0:y1, x0:x1]
    xyz_d50_pct = ecam.apply_camera_matrix(rgb_pct - ped, M)  # (H, W, 3)
    xyz_d65_pct = esh.adapt(xyz_d50_pct, ecam.D50, esh.D65_100)
    return xyz_d65_pct / 100.0


def unmix(xyz_abs: np.ndarray, *, swatch: bool = False) -> dict:
    """Per-pixel non-negative ink weights over the six inks, plus the residual.

    `xyz_abs` is (..., 3) absolute D65 XYZ in `pm.ink_xyz()`'s units. Converted to linear RGB with
    `eink_color.xyz_to_linear_rgb` — the same conversion `eink_barycentric.ink_vertices()` applies to
    the ink table, so pixel and vertex land in the same space — then decomposed with `decompose_
    clipped` (reused whole; see module docstring for why a second decomposer was not written).

    Returns `weights` (..., 6), `inside` (... bool, pre-clip hull membership) and `residual` (...,
    Euclidean distance in linear RGB between the pixel and its weighted reconstruction — ~0 for an
    exact, inside-hull fit; nonzero only where the point was clipped to the hull first).
    """
    shape = np.asarray(xyz_abs).shape[:-1]
    lin = ec.xyz_to_linear_rgb(xyz_abs).reshape(-1, 3)
    ctx = swatch_palette() if swatch else contextlib.nullcontext()
    with ctx:
        W, inside = eba.decompose_clipped(lin)
        vertices = eba.ink_vertices()
    recon = W @ vertices
    residual = np.linalg.norm(lin - recon, axis=1)
    return {
        "weights": W.reshape(*shape, 6),
        "inside": inside.reshape(shape),
        "residual": residual.reshape(shape),
    }


def ink_percentages(weights: np.ndarray) -> dict:
    """Area-weighted ink %, mean of per-pixel weights x100 — directly comparable to
    `eink_candidate.ink_fractions()`, which is a pixel-COUNT percentage over a digital index map."""
    flat = weights.reshape(-1, weights.shape[-1])
    return {name: round(100.0 * float(flat[:, i].mean()), 1) for i, name in enumerate(INK_NAMES)}


def residual_report(residual: np.ndarray, threshold: float, inside: np.ndarray | None = None) -> dict:
    """`pct_above_threshold` and `clipped_pct` answer DIFFERENT questions and both are kept (F1):
    `clipped_pct` is exactly "was this pixel outside the hull at all" (residual > 0, up to float noise
    — `decompose_clipped` only clips points `unmix` found `inside=False`); `pct_above_threshold` is "was
    the clip big enough to call the pixel unreliable". A majority-clipped, majority-below-threshold
    readout (V1/V2 before this fix) is a real but different fact from a majority-UNCLIPPED one, and the
    first version of this report conflated them by only reporting the second."""
    flat = residual.reshape(-1)
    out = {
        "mean": round(float(flat.mean()), 5),
        "median": round(float(np.median(flat)), 5),
        "p95": round(float(np.percentile(flat, 95)), 5),
        "max": round(float(flat.max()), 5),
        "threshold": round(threshold, 5),
        "pct_above_threshold": round(100.0 * float((flat > threshold).mean()), 2),
    }
    if inside is not None:
        out["clipped_pct"] = round(100.0 * (1.0 - float(np.asarray(inside).reshape(-1).mean())), 2)
    return out


def measured_readout(sh: esh.Shoot, tag: str, M: np.ndarray, ped: np.ndarray, threshold: float, *,
                      swatch: bool = False, flat_tag="auto") -> dict:
    xyz = frame_xyz_d65(sh, tag, M, ped, flat_tag=flat_tag)
    mix = unmix(xyz, swatch=swatch)
    return {
        "ink_pct": ink_percentages(mix["weights"]),
        "clipped_pct": round(100.0 * (1.0 - float(mix["inside"].reshape(-1).mean())), 2),
        "residual": residual_report(mix["residual"], threshold, mix["inside"]),
    }


#: Chosen and justified in the report: a FRACTION of the white ink's own linear-RGB norm, so the
#: threshold scales with whatever palette is decomposed against (measured or swatch) rather than being
#: a bare constant that silently means different things for the two. The fraction itself is set from
#: V2(B1)'s own p95 residual (review F6 — the first version asserted 8% "sits above the V2 noise floor"
#: while sitting BELOW V2's own max residual; re-derived here from a named quantile instead): see
#: `cmd_report`, which computes it from the actual B1 p95 and records the derivation in the JSON.
THRESHOLD_FRACTION_OF_WHITE = 0.08


def _white_norm(*, swatch: bool = False) -> float:
    ctx = swatch_palette() if swatch else contextlib.nullcontext()
    with ctx:
        vertices = eba.ink_vertices()
    return float(np.linalg.norm(vertices[INK_NAMES.index("white")]))


def default_threshold(*, swatch: bool = False, fraction: float = THRESHOLD_FRACTION_OF_WHITE) -> float:
    return fraction * _white_norm(swatch=swatch)


def derive_threshold_fraction(sh: esh.Shoot, M, ped, primaries_frame: str = "B1") -> dict:
    """The threshold's FRACTION-of-white's-norm, re-derived from V2(B1)'s own p95 residual (review F6)
    instead of the bare 8% the first version asserted without a stated basis. Pooling all six ink
    fields' pixel residuals (not per-field) gives one number that is not cherry-picked from whichever
    field happens to be cleanest."""
    x0, y0, x1, y1 = _content_box(sh)
    cw, ch = x1 - x0, y1 - y0
    full = frame_xyz_d65(sh, primaries_frame, M, ped)
    pooled = []
    for i in range(6):
        cx, cy = i % 3, i // 3
        rx0, ry0 = cx * cw // 3, cy * ch // 2
        rx1, ry1 = (cx + 1) * cw // 3, (cy + 1) * ch // 2
        dx, dy = int((rx1 - rx0) * 0.30), int((ry1 - ry0) * 0.30)
        sub = np.s_[ry0 + dy:ry1 - dy, rx0 + dx:rx1 - dx]
        pooled.append(unmix(full[sub])["residual"].reshape(-1))
    p95 = float(np.percentile(np.concatenate(pooled), 95))
    fraction = p95 / _white_norm()
    return {"basis": f"V2({primaries_frame}) pooled p95 residual across all six ink fields",
            "p95_residual": round(p95, 5), "fraction_of_white_norm": round(fraction, 4)}


# --- "D": the digital render's own index map, regenerated through the real CLI path -----------------
def digital_index(n: int, *, white_point: float = 0.0, gamma: float = 1.4, fit: str = "cover",
                   width: int = 1600, height: int = 1200) -> np.ndarray:
    """Regenerate `target art --n N ...` (via `tools.eink.eink_bench.cmd_target`, not re-implemented) and
    read back its own ink-index map from the saved PNG's content-box pixels.

    `cmd_target` writes RGB, not an index map, but the RGB it writes is PURE `SPECTRA6_OUTPUT_PALETTE`
    triples (re-encoded on purpose, see `eink_target._quantize`) with no resampling in that region
    (`eink_target.compose` pastes at native size) — so an exact-match lookup recovers the index
    losslessly without touching the quantiser's internals.
    """
    import time  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    import epaper as ep  # noqa: PLC0415
    from tools.eink import eink_bench as eb  # noqa: PLC0415
    from tools.eink import eink_target as et  # noqa: PLC0415

    ns = argparse.Namespace(
        kind="art", n=n, gamma=gamma, saturation=1.0, chroma_gamma=1.0, white_point=white_point,
        chroma_floor=0.0, chroma_floor_max=None, chroma_hue_e0=20.0, chroma_gap_normalised=False,
        chroma_floor_min=0.0, fit=fit, width=width, height=height, no_push=True,
        isolate=False, sat=0.55, v_lo=40, v_hi=245, centre=170,
    )
    before = time.time()
    eb.cmd_target(ns)
    matches = sorted(eb.OUT.glob(f"target_art{n:02d}_*_{width}x{height}.png"), key=lambda p: p.stat().st_mtime)
    if not matches:
        raise SystemExit(f"cmd_target did not produce target_art{n:02d}_*_{width}x{height}.png")
    dest = matches[-1]
    # F8: prove freshness rather than trust mtime ordering alone — a stale file left over from an
    # earlier run with the SAME tag (e.g. a previous white_point) would otherwise be picked silently.
    if dest.stat().st_mtime < before:
        raise SystemExit(f"{dest} predates this call (mtime {dest.stat().st_mtime} < {before}) — "
                          "cmd_target did not write a fresh file; refusing to read a stale render")
    img = np.asarray(Image.open(dest).convert("RGB"))
    x0, y0, x1, y1 = et.content_box(width, height)
    crop = img[y0:y1, x0:x1].reshape(-1, 3)
    pal = np.array(ep.SPECTRA6_OUTPUT_PALETTE)
    idx = np.full(len(crop), -1, dtype=np.int64)
    for i, c in enumerate(pal):
        idx[np.all(crop == c, axis=1)] = i
    unmatched = int((idx == -1).sum())
    if unmatched:
        raise SystemExit(f"{dest}: {unmatched} content-box pixels are not a pure output-palette colour "
                          "— compose()/quantize() must have changed; do not silently coerce them")
    return idx.reshape(y1 - y0, x1 - x0)


def digital_ink_fractions(n: int, **kw) -> dict:
    from tools.eink import eink_candidate as ecand  # noqa: PLC0415
    return ecand.ink_fractions(digital_index(n, **kw))


# --- validations ---------------------------------------------------------------------------------
def v1_flat(sh: esh.Shoot, M, ped, threshold: float, *, frame: str = "E2",
            self_check_frame: str = "E4") -> dict:
    """A settled white read through the manifest's OWN flat assignment must come back (near) pure
    white. `frame="E2"` (manifest `flat: "E4"`, a DIFFERENT exposure of the settling sequence) is the
    real validation: E2's own illumination gradient, vignetting, or any wrong flat frame would all show
    up here, because numerator and denominator are not the same shot.

    ⚠️ `E4`-divided-by-`E4` is kept as `self_check` but MUST NOT carry the V1 verdict (review F3): it is
    the same frame on both sides of the division, so a gradient, vignetting, or an outright wrong flat
    cancel by construction — it can only ever fail on M/pedestal/adaptation error at one near-white
    point, and is a self-consistency check, not a validation.
    """
    primary = measured_readout(sh, frame, M, ped, threshold, flat_tag="auto")
    self_check = measured_readout(sh, self_check_frame, M, ped, threshold, flat_tag=self_check_frame)
    return {"frame": frame, "flat": "auto (manifest default, E4)", **primary,
            "self_check_frame": self_check_frame,
            "self_check_note": "E4 divided by ITSELF — cannot fail for anything in the flat-field "
                                "stage; kept for reference only, not the V1 verdict",
            "self_check": self_check}


def v2_primaries(sh: esh.Shoot, M, ped, threshold: float, primaries_frame: str = "B1") -> dict:
    """Each of the six ink fields of a primaries frame, sampled the way `Shoot.inks` samples them (30%
    inset), read against the measured palette. The self-ink shortfall from 100% is an error bar (spec
    V2) — but ONLY if the frame is independent of the palette it is scored against.

    ⚠️ F1 is NOT independent (review F2): `_MEASURED_INK_XYZ` is F1's own six field means (x0.3762),
    so running this on F1 measures within-field NOISE around vertices that are those fields' means — a
    wrong camera matrix, wrong pedestal, or wrong D50->D65 adaptation cancels identically and F1's
    shortfall still reads small. `B1` (the independent bookend primaries frame, ADR-116 §3) is the
    accuracy bar; F1's own shortfall is kept as the noise floor and MUST be labelled as such, never
    quoted as the accuracy bar.
    """
    x0, y0, x1, y1 = _content_box(sh)
    cw, ch = x1 - x0, y1 - y0                    # frame_xyz_d65 is already cropped to this box
    full = frame_xyz_d65(sh, primaries_frame, M, ped)
    out = {}
    for i, name in enumerate(INK_NAMES):
        cx, cy = i % 3, i // 3
        rx0, ry0 = cx * cw // 3, cy * ch // 2
        rx1, ry1 = (cx + 1) * cw // 3, (cy + 1) * ch // 2
        dx, dy = int((rx1 - rx0) * 0.30), int((ry1 - ry0) * 0.30)
        sub = np.s_[ry0 + dy:ry1 - dy, rx0 + dx:rx1 - dx]
        xyz = full[sub]
        mix = unmix(xyz)
        pct = ink_percentages(mix["weights"])
        out[name] = {"self_pct": pct[name], "shortfall_pct": round(100.0 - pct[name], 1),
                     "clipped_pct": round(100.0 * (1.0 - float(mix["inside"].mean())), 2),
                     "residual": residual_report(mix["residual"], threshold, mix["inside"])}
    return out


def v3_bookend(sh: esh.Shoot, M, ped, threshold: float, ref: str = "F1", other: str = "B1") -> dict:
    """B1 vs F1 per-ink ΔE00 in media Lab (spec V3, the ADR-116 bar of <=2.6). B2/B2x are EXCLUDED —
    ADR-116 §3 found them ghosted (black-field std 9-12x the white's)."""
    def ink_lab_media(tag):
        d = sh.inks(tag)
        rgb = np.stack([d[n]["mean"] for n in INK_NAMES]) * 100.0
        col = esh.ink_colorimetry(rgb - ped, M)
        return col["lab_media"]
    lab_ref, lab_other = ink_lab_media(ref), ink_lab_media(other)
    de = ec.ciede2000(lab_other, lab_ref)
    worst_1dp = round(float(de.max()), 1)      # PRIMARIES_2026-09-19.md states the bar to 1 decimal
    return {"frames": [ref, other], "de00_per_ink": {n: round(float(d), 2) for n, d in zip(INK_NAMES, de)},
            "worst": round(float(de.max()), 2), "worst_1dp": worst_1dp, "bar": 2.6,
            "pass": bool(worst_1dp <= 2.6)}


def _content_box(sh: esh.Shoot):
    from tools.eink import eink_target as et  # noqa: PLC0415
    return et.content_box(sh.w, sh.h)


# --- CLI ---------------------------------------------------------------------------------------------
FRAMES = {
    "F6": {"art_n": 9, "white_point": 0.75, "gamma": 1.0, "what": "Sunflowers wp0.75 g1.0"},
    "F6b": {"art_n": 9, "white_point": 0.0, "gamma": 1.0, "what": "Sunflowers no wp"},
    "F7": {"art_n": 9, "white_point": 0.75, "gamma": 0.8, "what": "Sunflowers wp0.75 g0.8"},
    "F10": {"art_n": 52, "white_point": 0.75, "gamma": 1.0, "what": "Cafe Terrace n52 wp0.75"},
}

#: D, ESTABLISHED (third review pass, D1). `bench-eink/analysis/session_2026-09-20/{masterpieces__
#: sunflowers,masterpieces__caf-terrace-at-night}/inks.json` (key "A") is committed at 35d1069 —
#: ADR-120's OWN panel session, run on the bench Pi that actually does the pushing.
#: "A" is `epaper.render_for_epaper` byte-for-byte (swatch, wp 0.75, gamma 1.0, Floyd-Steinberg,
#: `eink_candidate.ink_fractions()` on its index map) — exactly F6's and F10's shipping recipe. A THIRD,
#: independent read-only check on the Pi today reproduced these numbers exactly and confirmed
#: `aspect_crops_json` is NULL for both works on the Pi too, `bench-eink/boxes.json` does not exist
#: there either, and `_authored_box(9)`/`_authored_box(52)` both return None — so there was never an
#: authored crop on EITHER machine to vary the render. The render path is deterministic and this IS
#: what was pushed. F6b (no white point) and F7 (gamma 0.8) were NOT independently re-verified on the
#: Pi in this pass — only F6's and F10's exact recipe was checked — so they are NOT in this table.
PI_ESTABLISHED_D = {
    "F6": {"black": 2.3, "white": 9.4, "red": 31.3, "yellow": 24.7, "blue": 0.0, "green": 32.3},
    "F10": {"black": 20.8, "white": 0.9, "red": 19.2, "yellow": 3.0, "blue": 17.3, "green": 38.7},
}
PI_ESTABLISHED_D_SOURCE = (
    "bench-eink/analysis/session_2026-09-20/{masterpieces__sunflowers,masterpieces__caf-terrace-at-"
    "night}/inks.json (key 'A'), committed 35d1069 (ADR-120's own bench-Pi session); independently "
    "reproduced by a read-only bench-Pi check in review pass 3"
)


def cmd_report(args) -> None:
    sh = esh.Shoot(args.manifest)
    out_path = Path(args.out)

    def write(obj):
        out_path.write_text(json.dumps(obj, indent=1))

    result = {"manifest": str(sh.path), "frames": {}}
    write(result)

    M, ped, fit = solve_matrix(sh, args.chart_frame)
    result["camera"] = {"M": M.tolist(), "pedestal_pct_fs": ped.tolist(),
                        "fit_mean_de00": fit["mean"], "fit_worst_de00": fit["worst"],
                        "reused": "tools.eink.eink_shoot.Shoot.chart_panel + eink_camera.solve_camera_affine "
                                  "(frame X2, the on-panel ColorChecker) — identical to `eink_shoot "
                                  "primaries`'s camera-matrix step"}
    write(result)

    # F6: the threshold's FRACTION is re-derived from a named quantile (V2(B1)'s pooled p95 residual),
    # not asserted. A bootstrap threshold (any value) is needed to compute V2(B1) itself since the
    # residual arrays it returns don't depend on the threshold at all (only pct_above_threshold does).
    deriv = derive_threshold_fraction(sh, M, ped, "B1")
    fraction = deriv["fraction_of_white_norm"]
    threshold = default_threshold(fraction=fraction)
    threshold_swatch = default_threshold(swatch=True, fraction=fraction)
    result["threshold"] = {
        "measured": threshold, "swatch": threshold_swatch, "fraction_of_white_norm": fraction,
        "derivation": deriv,
        "definition": f"{fraction:.1%} of the white ink's own linear-RGB norm (the fraction is "
                       "V2(B1)'s pooled p95 residual / white's norm), in whichever palette is "
                       "decomposed against",
    }
    write(result)

    result["V1_flat"] = v1_flat(sh, M, ped, threshold)
    write(result)
    result["V2_noise_floor_F1"] = v2_primaries(sh, M, ped, threshold, primaries_frame="F1")
    result["V2_noise_floor_F1_note"] = ("F1 DEFINES _MEASURED_INK_XYZ (its own field means x0.3762) — "
                                        "this is within-field noise around vertices that ARE those "
                                        "fields' means, not an accuracy bar (review F2). Kept for "
                                        "reference; do not quote as the bar.")
    write(result)
    result["V2_accuracy_bar_B1"] = v2_primaries(sh, M, ped, threshold, primaries_frame="B1")
    result["V2_accuracy_bar_B1_note"] = ("B1 is the independent bookend primaries frame (ADR-116 §3) — "
                                         "not used to define the ink table, so its shortfall is a real "
                                         "accuracy bar. F1's and B1's differ because F1 is circular "
                                         "(see the noise-floor note) and B1 is not.")
    bar = {n: result["V2_accuracy_bar_B1"][n]["shortfall_pct"] for n in INK_NAMES}
    result["V2_bar_caveat"] = ("the bar is measured on PURE ink fields (B1) and applied below to "
                               "BLURRED mixtures (the art frames) — it is a LOWER bound on error "
                               "there, not a true error bar for mixed content")
    write(result)
    result["V3_bookend"] = v3_bookend(sh, M, ped, threshold)
    write(result)

    # D is ESTABLISHED for F6/F10 (review pass 3, D1/D2) — the crop-mismatch story from pass 2 is DEAD:
    # there was never an authored crop for either work on EITHER machine (laptop or the bench Pi that
    # actually does the pushing), so the crop mechanism explains nothing and cannot have caused
    # anything. `PI_ESTABLISHED_D` above is the committed Pi session artefact; F6b/F7 have no such
    # independent verification and stay uncertain, with a NEW reason (not re-verified on the Pi for
    # their specific recipe), not the old (dead) crop-mismatch reason.
    adr117_f6 = {"black": 25.0, "white": 6.0, "red": 13.0, "yellow": 48.0, "green": 8.0}
    adr117_note = (
        "ADR-117's Sunflowers shipping fractions (black 25 / white 6 / red 13 / yellow 48 / green 8) "
        "are UNREPRODUCIBLE by any framing tested: not the Pi today (established D, above), not "
        "ADR-120's own recorded session (which already silently contradicted it — 24.7/9.4/2.3 "
        "yellow/white/black vs ADR-117's 48/6/25, unnoticed until now), not this laptop's regeneration, "
        "and not a portrait-framed render (752x960, tested and REFUTED as a hypothesis: yields yellow "
        "27.2, not 48). Do not use ADR-117's Sunflowers fractions as a yardstick until someone "
        "reproduces them. OPEN QUESTION, NOT A FINDING, explicitly a guess: two of its five inks sit "
        "close to the MEASURED PHOTOGRAPH's own readout (black 25 vs P 25.0, red 13 vs P 12.6, see the "
        "F6 frame below) — this might mean those figures were a photograph classification rather than "
        "a render, but this is unverified and not chased further here."
    )
    result["D_ground_truth"] = {
        "verdict": "ESTABLISHED for F6 and F10 via the committed Pi session artefact (see "
                   "PI_ESTABLISHED_D_SOURCE); NOT independently verified for F6b/F7's specific recipe.",
        "established": PI_ESTABLISHED_D, "established_source": PI_ESTABLISHED_D_SOURCE,
        "adr117_reported_F6_shipping": adr117_f6, "adr117_note": adr117_note,
        "open_items": [
            "laptop-vs-Pi ~1pt delta on F6 (this laptop's local regeneration: black 2.3/white 8.8/red "
            "32.2/yellow 23.7/green 33.0 vs the Pi's established 2.3/9.4/31.3/24.7/32.3) on a path "
            "that is deterministic Floyd-Steinberg and SHOULD not differ at all — under the B1 bar, "
            "but unexplained; likely the checkout delta (laptop 24f9841 vs Pi c5bf202) or a "
            "library-image difference. Not investigated this pass.",
            "`eink_bench._db_crop_and_focal` returned focal (0.5, 0.5) on the Pi for n=9 while DB row "
            "id 14 carries focal_x 0.5, focal_y 0.42 — the filename lookup appears to miss and silently "
            "fall back to centre. Possible separate bug, flagged not fixed.",
        ],
    }
    write(result)

    for tag, cfg in FRAMES.items():
        if tag in PI_ESTABLISHED_D:
            d, d_uncertain = PI_ESTABLISHED_D[tag], False
            d_note = f"established: {PI_ESTABLISHED_D_SOURCE}"
        else:
            d_uncertain = True
            d_note = ("NOT independently verified on the Pi for this recipe (only F6's/F10's shipping "
                      "recipe was Pi-checked, review pass 3). Local laptop regeneration shown for "
                      "reference only.")
            try:
                d = digital_ink_fractions(cfg["art_n"], white_point=cfg["white_point"], gamma=cfg["gamma"])
            except Exception as e:  # noqa: BLE001 — reported, not swallowed
                d, d_note = None, f"regeneration failed: {e}"
        p = measured_readout(sh, tag, M, ped, threshold)
        s = measured_readout(sh, tag, M, ped, threshold_swatch, swatch=True)
        # F5 (pass 2): apply the (B1) accuracy bar to every P-vs-S difference, mechanically.
        p_vs_s = {}
        for name in INK_NAMES:
            diff = round(abs(p["ink_pct"][name] - s["ink_pct"][name]), 1)
            p_vs_s[name] = {"P": p["ink_pct"][name], "S": s["ink_pct"][name], "diff": diff,
                            "bar_pts": bar[name], "below_resolution": bool(diff < bar[name])}
        # D3 (pass 3): D-vs-P, the comparison the report could not make until D was established. Still
        # computed (and reported, never suppressed) for F6b/F7 even though D is uncertain there.
        d_vs_p = None
        if d is not None:
            d_vs_p = {}
            for name in INK_NAMES:
                diff = round(abs(d[name] - p["ink_pct"][name]), 1)
                d_vs_p[name] = {"D": d[name], "P": p["ink_pct"][name], "diff": diff,
                                "bar_pts": bar[name], "below_resolution": bool(diff < bar[name])}
        result["frames"][tag] = {
            "what": cfg["what"], "D": d, "D_uncertain": d_uncertain, "D_note": d_note,
            "P": p["ink_pct"], "P_clipped_pct": p["clipped_pct"], "P_residual": p["residual"],
            "S": s["ink_pct"], "S_clipped_pct": s["clipped_pct"], "S_residual": s["residual"],
            "P_vs_S": p_vs_s,
            "P_vs_S_note": ("the swatch's residual asymmetry (S clips/exceeds threshold far more than "
                            "P) is STRUCTURALLY expected, not independent evidence (review F7): the "
                            "measured hull was fit through this exact chain on this exact photograph, "
                            "so P's pixels nearly must lie inside it, while the swatch's hull had no "
                            "such privilege. Treat the residual gap as CONSISTENT WITH the swatch "
                            "fitting worse, not as proof of it."),
            "D_vs_P": d_vs_p,
            "D_vs_P_note": ("D is a DIGITAL RENDER; P is a PHOTOGRAPH through the full measurement "
                            "chain (V1/V2's own drift/noise caveats apply here too) — do not present "
                            "D-vs-P as pure renderer error. The bar (B1, pure ink fields) is a LOWER "
                            "bound on error for the blurred mixtures below."),
        }
        write(result)

    print(f"-> {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("report", help="V1/V2/V3 + the D/P/S triple for F6/F6b/F7/F10")
    p.add_argument("manifest")
    p.add_argument("--chart-frame", default="X2")
    p.add_argument("--out", default="bench-eink/analysis/frame_inks_2026-09-19.json")
    args = ap.parse_args()
    {"report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
