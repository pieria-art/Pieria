"""
tools/eink_readout.py — turn a corrected photograph of a target into numbers
(maintainer tool — NOT part of the runtime image).

⚠️ WHAT THESE NUMBERS ARE. Every value here is in CAMERA-RGB NORMALISED TO THIS PANEL'S OWN BLACK
AND WHITE — not sRGB. The correction upstream is a per-channel affine anchored on the black and white
patches, which absorbs exposure and gross white balance but does NOT characterise the camera's
spectral response. So:

  * A-vs-B comparisons on the same panel are valid, and that is where the shipping decision lives.
  * Absolute colour claims are NOT valid. Directions are probably meaningful, magnitudes are not.
    In particular this is NOT licence to rewrite SPECTRA6_DITHER_PALETTE.

⚠️ STRUCTURE BEATS COLOUR ON THIS RIG. The normalisation is a GLOBAL affine, so it distorts means
but leaves LOCAL structure intact. Variance, texture, anisotropy and modulation readouts are
therefore substantially more robust than mean-colour readouts, and are not bounded by the rig's
~6-8/255 mean accuracy. Prefer them where a question can be asked either way.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import eink_target as et  # noqa: E402


def _cells(img: Image.Image, w: int, h: int, cols: int, rows: int, gutter: int = 8) -> list:
    """Per-cell arrays for a cols x rows grid laid out by et._grid_rects, in content coordinates."""
    x0, y0, x1, y1 = et.content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    a = np.asarray(img.convert("RGB")).astype(float)
    out = []
    for rx0, ry0, rx1, ry1 in et._grid_rects(cw, ch, cols, rows, gutter):
        # Inset hard: the fusion kernel is 6x6 panel px and error diffusion contaminates a cell's
        # leading edge, so the readout is taken from the interior only.
        dx, dy = int((rx1 - rx0) * 0.22), int((ry1 - ry0) * 0.22)
        out.append(a[y0 + ry0 + dy: y0 + ry1 - dy, x0 + rx0 + dx: x0 + rx1 - dx])
    return out


#: Chroma below this is indistinguishable from the rig's own error, so a hue angle computed from it
#: is noise wearing a number's clothes. The rig agrees with itself to ~6/255 on whites and ~8/255 on
#: darks; anything under that cannot support a direction. Measured consequence of getting this wrong:
#: a neutral ramp reported a 303-degree "hue rotation" that was entirely quantisation noise.
HUE_MIN_CHROMA = 8.0


def _hue_angle(rgb) -> float:
    """Hue in degrees from a mean RGB triple. Returns NaN when the colour is too neutral for a hue
    to be meaningful — reporting an angle for a grey is reporting noise as a measurement."""
    r, g, b = [float(v) for v in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < HUE_MIN_CHROMA:
        return float("nan")
    return math.degrees(math.atan2(math.sqrt(3) * (g - b), 2 * r - g - b)) % 360.0


def _grain(cell: np.ndarray) -> float:
    """Local standard deviation inside a flat cell = the DITHER GRAIN.

    This is the cost side of white-point compression: a flat area above the white ink's luminance
    renders as clean flat white, and pushing it below the ceiling forces it to be built from black
    and white dots instead. A judge described that trade on a bronze statue; this measures it.
    """
    g = cell.mean(axis=2)
    return float(g.std())


def _anisotropy(cell: np.ndarray) -> float:
    """Row-variance vs column-variance of a flat cell.

    Floyd-Steinberg propagates error rightward and downward, so when it locks into a periodic
    structure ("worming") the texture is directional. A value near 1.0 is isotropic grain; far from
    1.0 is structure. Mean level cannot see this at all.
    """
    g = cell.mean(axis=2)
    rv = float(g.mean(axis=1).var())
    cv = float(g.mean(axis=0).var())
    return float(rv / cv) if cv > 1e-9 else float("nan")


def readout_grid_means(img, w, h, cols, rows, gutter=8) -> dict:
    cells = _cells(img, w, h, cols, rows, gutter)
    return {"cells": [[round(float(v), 2) for v in c.reshape(-1, 3).mean(axis=0)] for c in cells],
            "grain": [round(_grain(c), 2) for c in cells]}


def readout_inkmix(img, w, h) -> dict:
    """The optical mixing law: measured colour for every ink pair at every known ratio.

    ⚠️ CAVEAT ON `linearity_error`: its pure-ink reference comes from the calibration STRIP, and strip
    patches (250x96, embedded in a bright canvas) measure differently from large ink FIELDS
    (509x345, clean interiors) — measured 2026-08-29 at 40-81/255 apart in the chromatic inks, in raw
    pre-affine values. Black and white are exempt because the affine is anchored on them, which also
    means their agreement proves nothing. So treat linearity_error as provisional; the `mixtures`
    themselves are the primary data and it can be recomputed offline against better pure references.
    The patch-size effect is itself measured by the element-size sweep in this same target.

    `linearity_error` is the headline. If the panel mixed additively, a 1:1 tile of inks A and B
    would measure at the midpoint of the two pure inks. This reports how far off that is, per pair,
    in panel-relative units — and it is the number that decides whether the renderer's sRGB distance
    model is sound or whether every quantiser decision is built on a false premise.
    """
    inks = list(range(6))
    pairs = [(i, j) for i in inks for j in inks if i < j]
    cols, rows = len(pairs), 7
    cells = _cells(img, w, h, cols, rows, gutter=6)
    ratios = (0.125, 0.25, 0.5, 0.75, 0.875)
    mix, lin = {}, {}
    pure = readout_primaries_from_strip(img, w, h)
    for c, (ia, ib) in enumerate(pairs):
        key = f"{et.INK_NAMES[ia]}+{et.INK_NAMES[ib]}"
        vals = []
        for r in range(len(ratios)):
            vals.append([round(float(v), 2) for v in cells[r * cols + c].reshape(-1, 3).mean(axis=0)])
        mix[key] = {"ratios": list(ratios), "measured": vals}
        pa, pb = pure.get(et.INK_NAMES[ia]), pure.get(et.INK_NAMES[ib])
        if pa and pb:
            worst = 0.0
            for r, f in enumerate(ratios):
                pred = [f * pa[k] + (1 - f) * pb[k] for k in range(3)]
                worst = max(worst, max(abs(pred[k] - vals[r][k]) for k in range(3)))
            lin[key] = round(worst, 2)
    element = [[round(float(v), 2) for v in cells[5 * cols + c].reshape(-1, 3).mean(axis=0)]
               for c in range(cols)]
    metamer = [[round(float(v), 2) for v in cells[6 * cols + c].reshape(-1, 3).mean(axis=0)]
               for c in range(cols)]
    return {"mixtures": mix, "linearity_error": lin,
            "element_sweep": element, "metamer_row": metamer,
            "worst_linearity_error": round(max(lin.values()), 2) if lin else None}


def readout_primaries_from_strip(img, w, h) -> dict:
    """The six pure inks as measured from the calibration strip in the SAME photograph.

    Every target carries this strip, so every capture re-measures the inks. Comparing the strip's
    reading against a large content field of the same ink is the rig's own error bar — the two are
    the same ink in two places, so their disagreement is instrument error, not panel behaviour.
    """
    from tools import eink_measure as em  # local import: readout is importable without a camera
    rects = em.patch_rects(w, h, len(et.INK_NAMES))
    return {n: [round(float(v), 2) for v in em._mean_rgb(img, r)]
            for n, r in zip(et.STRIP_ORDER, rects)}


def readout_primaries(img, w, h) -> dict:
    """Large ink fields plus the strip, and the disagreement between them = instrument error."""
    from tools import eink_measure as em  # noqa: PLC0415
    fields = em.measured_primaries(img, w, h)
    strip = readout_primaries_from_strip(img, w, h)
    agree = {n: round(max(abs(a - b) for a, b in zip(fields[n], strip[n])), 2)
             for n in et.INK_NAMES if n in fields and n in strip}
    return {"fields": fields, "strip": strip, "field_vs_strip": agree,
            "worst_disagreement": round(max(agree.values()), 2) if agree else None}


def readout_tonefine(img, w, h, lo=100, hi=200, steps=26) -> dict:
    """Tone response, dither grain, worming anisotropy and NEUTRAL-AXIS HUE ROTATION from one frame.

    ⚠️ THE HUE READOUT IS A TEST OF THE PALETTE, NOT OF THE DITHER. The tempting story is that a
    dithered "grey" must rotate in hue with level, because it is built from different ink sets at
    different levels (dark greys lean on black+blue+green, bright greys on white). Measured digitally
    on 2026-08-29, that story is FALSE: the ink mix does swing hard (green 0.40 -> 0.51 -> 0.00 and
    white 0.00 -> 1.00 across input 60-230) but the resulting colour stays neutral to within
    4.0/255 — which is just the white ink's own faint blue at (161,164,165). Floyd-Steinberg
    recombines the inks back onto the neutral axis because that is precisely the error it minimises.

    What survives, and why this readout is still worth taking: FS lands on neutral only if
    SPECTRA6_DITHER_PALETTE describes THIS panel's inks. It is Pimoroni's measurement of a different
    EL133UF1. If our inks sit elsewhere, the dither is optimising toward the wrong targets and
    neutrality breaks ON GLASS while looking perfect in simulation. So a hue rotation measured here
    is evidence about the PALETTE, and `inkmix` plus `primaries` are what localise it.
    """
    cells = _cells(img, w, h, 13, 2, gutter=4)
    out = []
    for i in range(min(steps, len(cells))):
        v_in = round(lo + (hi - lo) * i / max(steps - 1, 1))
        c = cells[i]
        mean = c.reshape(-1, 3).mean(axis=0)
        ha = _hue_angle(mean)
        out.append({"in": v_in,
                    "out_rgb": [round(float(x), 2) for x in mean],
                    "out_lum": round(float(mean.mean()), 2),
                    "chroma": round(float(mean.max() - mean.min()), 2),
                    "grain": round(_grain(c), 2),
                    "anisotropy": round(_anisotropy(c), 3),
                    "hue_deg": (round(ha, 1) if not math.isnan(ha) else None)})
    # Only steps whose chroma clears HUE_MIN_CHROMA can contribute a direction — see that constant.
    hues = [s["hue_deg"] for s in out if s["hue_deg"] is not None]
    lums = [s["out_lum"] for s in out]
    collapsed = sum(1 for a, b in zip(lums, lums[1:]) if abs(b - a) < 2.0)
    return {"steps": out,
            "hue_qualifying_steps": len(hues),
            "max_chroma_on_neutral_axis": round(max(s["chroma"] for s in out), 2) if out else None,
            "neutral_hue_range_deg": (round(max(hues) - min(hues), 1) if len(hues) > 1 else None),
            "collapsed_step_pairs": collapsed,
            "grain_peak": round(max(s["grain"] for s in out), 2) if out else None}


def readout_huevalue(img, w, h, hues=12, values=6, sat=0.55) -> dict:
    """ADR-091's table, measured: does chroma survival really collapse as value rises?

    Reports measured chroma per (hue, value) cell as a fraction of the chroma actually requested.
    ADR-091 predicts (by SIMULATION, never checked on glass) that every hue survives at v=100 and
    that six collapse to zero by v=220. This either reproduces that or it does not.
    """
    cells = _cells(img, w, h, hues, values, gutter=6)
    rows = []
    for r in range(values):
        v_in = round(40 + (245 - 40) * r / max(values - 1, 1))
        row = []
        for c in range(hues):
            m = cells[r * hues + c].reshape(-1, 3).mean(axis=0)
            chroma_out = float(m.max() - m.min())
            row.append({"hue_in_deg": round(360.0 * c / hues, 1),
                        "chroma_out": round(chroma_out, 2),
                        "lum_out": round(float(m.mean()), 2),
                        "hue_out_deg": (round(_hue_angle(m), 1)
                                        if not math.isnan(_hue_angle(m)) else None)})
        rows.append({"value_in": v_in, "cells": row,
                     "n_collapsed": sum(1 for c in row if c["chroma_out"] < 6.0)})
    return {"saturation_in": sat, "rows": rows}


def readout_surround(img, w, h, centre=170) -> dict:
    """Does an identical input measure the same in 25 different surrounds?

    The spread IS the answer. If it exceeds the rig's repeatability floor, then every dithered grid
    target in this battery carries an unquantified surround term and its cell values are "V given its
    neighbours" rather than "the panel's response to V".
    """
    cells = _cells(img, w, h, 5, 5, gutter=4)
    means = []
    for c in cells[:25]:
        ih, iw = c.shape[0], c.shape[1]
        core = c[ih // 4: ih - ih // 4, iw // 4: iw - iw // 4]
        means.append(round(float(core.reshape(-1, 3).mean(axis=0).mean()), 2))
    return {"centre_in": centre, "centre_out": means,
            "spread": round(max(means) - min(means), 2),
            "sd": round(float(np.std(means)), 2)}


def readout_edges(img, w, h) -> dict:
    """Error-diffusion smear: is the trailing side of an edge different from the leading side?

    FS pushes error right and down. `asymmetry` is (mean of the strip just right of the inner block)
    minus (mean of the strip just left of it), in panel-relative units. A value at the noise floor
    means diffusion is not visibly directional at this contrast.
    """
    cells = _cells(img, w, h, 4, 2, gutter=6)
    out = []
    for c in cells[:8]:
        hgt, wid = c.shape[0], c.shape[1]
        band = c[hgt // 3: 2 * hgt // 3]
        strip = max(4, wid // 12)
        left = float(band[:, :strip].mean())
        right = float(band[:, -strip:].mean())
        out.append({"left": round(left, 2), "right": round(right, 2),
                    "asymmetry": round(right - left, 2)})
    worst = max((abs(o["asymmetry"]) for o in out), default=0.0)
    return {"blocks": out, "worst_asymmetry": round(worst, 2)}


def readout_linepairs(img, w, h) -> dict:
    """Modulation depth per period and orientation — what detail survives the dither.

    Periods below 8 px are absent by construction: at ~0.86 camera px per panel px they would report
    the camera's MTF rather than the panel's.
    """
    cells = _cells(img, w, h, 6, 6, gutter=5)
    periods = (8, 12, 16, 24, 32, 48)
    orients = ("h", "v", "d")
    contrasts = ((110, 190), (60, 240))
    out, i = [], 0
    for lo, hi in contrasts:
        for orient in orients:
            group = []
            for p in periods:
                c = cells[i]; i += 1
                g = c.mean(axis=2)
                # Average ALONG the lines so the profile is across them. A diagonal has no such
                # axis, so its modulation is read from the 2D distribution instead of a profile.
                if orient == "h":
                    prof = g.mean(axis=1)
                elif orient == "v":
                    prof = g.mean(axis=0)
                else:
                    prof = g.reshape(-1)
                mod = float(np.percentile(prof, 90) - np.percentile(prof, 10))
                group.append({"period_px": p, "orientation": orient,
                              "contrast_in": hi - lo, "modulation_out": round(mod, 2)})
            # ⚠️ Normalise against the COARSEST period of this same orientation and contrast, not
            # against the input contrast. Output modulation legitimately EXCEEDS input contrast here
            # — a dithered mid-grey is built from PURE inks, so the local swing is black-to-white
            # regardless of how gentle the requested contrast was. Dividing by contrast_in produced
            # "retained" values above 1.4, which is not a detail-retention figure at all. Against the
            # coarsest period the number means what it should: 1.0 = this period is resolved as well
            # as a period the panel certainly resolves, 0 = gone.
            ref = max((b["modulation_out"] for b in group), default=0.0)
            for b in group:
                b["retained"] = round(b["modulation_out"] / ref, 3) if ref > 1e-6 else None
            out.extend(group)
    return {"blocks": out, "normalisation": "modulation relative to the coarsest period of the "
                                            "same orientation and contrast"}


def readout_resample(img, w, h) -> dict:
    """Texture modulation at three source scales — resampler loss vs panel loss."""
    cells = _cells(img, w, h, 3, 1, gutter=8)
    out = []
    for si, scale in enumerate((1, 2, 4)):
        g = cells[si].mean(axis=2)
        out.append({"source_scale": scale,
                    "modulation": round(float(np.percentile(g, 90) - np.percentile(g, 10)), 2),
                    "sd": round(float(g.std()), 2)})
    return {"scales": out}


def readout_uniformity(img, w, h) -> dict:
    """Each ink at nine positions. ⚠️ Meaningless from ONE capture — see target_uniformity."""
    macros = _cells(img, w, h, 3, 3, gutter=6)
    grid = []
    for m in macros:
        mh, mw = m.shape[0], m.shape[1]
        inks = []
        for i in range(6):
            c, r = i % 3, i // 3
            sub = m[r * mh // 2: (r + 1) * mh // 2, c * mw // 3: (c + 1) * mw // 3]
            ih, iw = sub.shape[0], sub.shape[1]
            core = sub[ih // 4: ih - ih // 4, iw // 4: iw - iw // 4]
            inks.append([round(float(v), 2) for v in core.reshape(-1, 3).mean(axis=0)])
        grid.append(inks)
    spread = {}
    for i, name in enumerate(et.INK_NAMES):
        lums = [sum(pos[i]) / 3.0 for pos in grid]
        spread[name] = round(max(lums) - min(lums), 2)
    return {"positions": grid, "luminance_spread_by_ink": spread,
            "note": "REQUIRES a paired capture with the panel rotated 180 degrees to separate "
                    "panel non-uniformity from rig flat-field residual."}


READOUTS = {
    "primaries": readout_primaries,
    "inkmix": readout_inkmix,
    "uniformity": readout_uniformity,
    "tonefine": readout_tonefine,
    "huevalue": readout_huevalue,
    "surround": readout_surround,
    "edges": readout_edges,
    "linepairs": readout_linepairs,
    "resample": readout_resample,
}
