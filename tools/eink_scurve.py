"""
tools/eink_scurve.py — fit and evaluate an S-shaped tone curve for the Spectra 6 panel
(maintainer tool — NOT part of the runtime image).

WHY AN S-CURVE. The panel is starved at BOTH ends of the tone range, and it is the same palette fact
twice: exactly ONE ink above luminance 101 (white, 163) and exactly ONE below 71 (black, 0). Content
pushed past either end stops being modelled at all.

Every lever measured in the 2026-08-29 corpus is a PURE POWER FUNCTION or a LINEAR SCALE:

    white-point   a scale        y = wp * x
    gamma         a power        y = x ** g

Both move the WHOLE range in one direction, so they can only ever trade one collapse for the other.
Measured on The Night Watch, fraction of the shadow region rendering as bare black ink: 67.4% with the
white-point off, rising to 78.1% at wp 0.64 — compression makes shadows worse, monotonically, and the
entire useful span of that lever is ~11 points. That is a limitation of the CURVE FAMILY, not of the
panel, and no member of that family can fix both ends at once.

An S-curve has INDEPENDENT ends: a toe that lifts shadows off the black ink, and a shoulder that holds
highlights under the white ink. Three parameters, all interpretable:

    pivot     the input level that stays where it is
    toe       > 0 lifts shadows (exponent below 1 under the pivot)
    shoulder  > 0 compresses highlights (exponent above 1 over the pivot)

⚠️ IT IS SCORED BY SIMULATION, NOT BY A FITTED MODEL. The renderer is deterministic, so for any curve
the exact ink each pixel receives can be computed offline. Nothing here depends on the measured
transfer function, which carries an unresolved +26/255 row term and reads flat below digital 40 where
the render is provably linear. Where the corpus and the simulation disagree, the simulation is the one
that can be checked.

⚠️ WHAT THIS CANNOT DECIDE. Whether a lifted black point — no true black anywhere in the frame — is
acceptable on a wall is a judgement, not a measurement (ADR-084: the panel decides). The optimiser
reports it; it does not weigh it.

🔴 `cost()` IS WITHDRAWN AS A FITTING OBJECTIVE (ADR-097, 2026-08-29). Gated against the 23 human
white-point calls it scores 7/23 = 30.4% against a 34.8% base rate — and, disqualifyingly, **predicts
wp 0.64 on 23 of 23 works**: it is a constant, and its hits are the works where the judge happened to
agree with the constant. The highlight term outweighs everything opposing it by ~4x, so compression
always wins; the same mechanism ran the toe to the grid edge. On the six fitted works this cost
prefers the degenerate curve to the identity LUT 90.53 vs 224.20. The best possible re-weighting of
the six terms reaches only 34.8-43.5% leave-one-out, so **do not re-tune the weights** — the terms
cannot see what the judge sees. Gate any replacement with `tools/eink_objective_gate.py` FIRST;
detail in `bench-eink/analysis/OBJECTIVE_GATE_2026-08-29.md`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import epaper as ep  # noqa: E402

DITHER = np.array(ep.SPECTRA6_DITHER_PALETTE, dtype=float)
INK_LUM = DITHER.mean(axis=1)
BLACK_IDX, WHITE_IDX = 0, 1


def scurve_lut(pivot: float = 0.45, toe: float = 0.0, shoulder: float = 0.0) -> list:
    """A 256-entry LUT for the S-curve. pivot/toe/shoulder as described in the module docstring.

    toe=shoulder=0 is the identity, so the family CONTAINS "do nothing" and the optimiser can always
    fall back to it. That matters: a curve family that cannot express the null hypothesis will always
    appear to help.
    """
    p = max(0.02, min(0.98, pivot))
    lut = []
    for i in range(256):
        x = i / 255.0
        if x <= p:
            y = p * (x / p) ** (1.0 / (1.0 + toe)) if x > 0 else 0.0
        else:
            y = p + (1.0 - p) * ((x - p) / (1.0 - p)) ** (1.0 + shoulder)
        lut.append(int(round(max(0.0, min(1.0, y)) * 255)))
    return lut


def render_indices(img: Image.Image, lut=None) -> np.ndarray:
    """Ink index per pixel, through the production quantiser. This is what the panel receives."""
    src = img.point(lut * 3) if lut is not None else img
    q = src.quantize(palette=ep._cached_palette_image("_spectra6_dither", ep.SPECTRA6_DITHER_PALETTE),
                     dither=Image.Dither.FLOYDSTEINBERG)
    return np.asarray(q)


def score(img: Image.Image, lut=None) -> dict:
    """Measured consequences of a curve on one work. Every term is counted, not modelled."""
    a = np.asarray(img.convert("RGB")).astype(float)
    lum = a.mean(axis=2)
    chroma = a.max(axis=2) - a.min(axis=2)
    idx = render_indices(img, lut)
    out = DITHER[idx]

    shadow = lum < 60
    highlight = lum > 195
    pale_col = (lum > 170) & (chroma > 18)      # the rose, warm sheets — colour at risk of the ceiling

    def frac(mask, i):
        return float((idx[mask] == i).mean() * 100) if mask.sum() else float("nan")

    out_ch = out.max(axis=2) - out.min(axis=2)
    # ⚠️ FIDELITY. Without this the objective is GAMEABLE, and was gamed: collapse metrics count
    # pixels reaching the extreme inks, so a curve that crushes the whole range into a band of
    # mid-grey drives both to zero and "wins" with a flat, contrastless image. Measured: the
    # unconstrained optimum ran to toe=shoulder=11.66, where the shoulder exponent is 12.66 and
    # everything above the pivot maps onto the pivot. REWARDING THE ABSENCE OF FAILURE IS NOT THE
    # SAME AS REWARDING A GOOD PICTURE.
    #
    # The panel's white ink is the ceiling, so a faithful render maps source 0..255 onto ink 0..163.
    # Deviation from that line is tone error, and it is what stops the optimiser cheating.
    src_n = lum / 255.0
    out_n = out.mean(axis=2) / float(INK_LUM[WHITE_IDX])
    tone_error = float(np.abs(out_n - src_n).mean() * 100)
    contrast_kept = float(out.mean(axis=2).std() / max(lum.std(), 1e-6))
    return {
        "tone_error": tone_error,
        "contrast_kept": contrast_kept,
        "shadow_to_black": frac(shadow, BLACK_IDX),
        "highlight_to_white": frac(highlight, WHITE_IDX),
        "pale_chroma_kept": float(out_ch[pale_col].mean()) if pale_col.sum() else float("nan"),
        "pale_to_white": frac(pale_col, WHITE_IDX),
        # grain: local structure on flat regions. B1 measured that grain follows where tone LANDS,
        # not which lever put it there, so it is scored on the output rather than on the settings.
        "grain": float(np.abs(np.diff(out.mean(axis=2), axis=0)).mean()),
        "true_black_pct": float((idx == BLACK_IDX).mean() * 100),
        "distinct_inks": int(len(np.unique(idx))),
    }


def cost(s: dict, w_shadow=1.0, w_high=1.0, w_chroma=1.0, w_grain=0.35,
         w_tone=3.0, w_contrast=40.0) -> float:
    """One number to minimise. Collapse terms are percentages; chroma is a loss from its own ceiling.

    ⚠️ The weights are a VALUE JUDGEMENT, not a measurement. They are exposed so the trade can be
    argued about explicitly rather than buried in a scalar.
    """
    sh = 0.0 if np.isnan(s["shadow_to_black"]) else s["shadow_to_black"]
    hi = 0.0 if np.isnan(s["highlight_to_white"]) else s["highlight_to_white"]
    ch = 0.0 if np.isnan(s["pale_chroma_kept"]) else max(0.0, 40.0 - s["pale_chroma_kept"])
    lost_contrast = max(0.0, 1.0 - s["contrast_kept"])
    return (w_shadow * sh + w_high * hi + w_chroma * ch + w_grain * s["grain"]
            + w_tone * s["tone_error"] + w_contrast * lost_contrast)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+")
    ap.add_argument("--max-px", type=int, default=700)
    ap.add_argument("--out", default="bench-eink/analysis/scurve_fit.json")
    ap.add_argument("--baseline-wp", type=float, default=0.75)
    ap.add_argument("--baseline-gamma", type=float, default=1.0)
    args = ap.parse_args()

    works = []
    for p in args.images:
        f = Path(p)
        if not f.exists():
            print(f"  skip (missing): {f.name}")
            continue
        im = Image.open(f).convert("RGB")
        im.thumbnail((args.max_px, args.max_px))
        works.append((f.stem[:44], im))
    print(f"{len(works)} works\n")

    def eval_lut(lut):
        rows = [score(im, lut) for _, im in works]
        return rows, float(np.mean([cost(r) for r in rows]))

    # --- the incumbents, so any S-curve must beat something real -----------------------------------
    base = {}
    for label, lut in (
            ("production (adaptive gamma, no wp)", None),   # replaced per-work below
            ("wp0.75 g1.0 (recipe candidate)",
             [min(255, int(round(i * args.baseline_wp))) for i in range(256)]),
            ("no correction", list(range(256)))):
        if lut is None:
            rows = []
            for _, im in works:
                g = ep._adaptive_gamma(im)
                rows.append(score(im, [round(255 * (i / 255) ** g) for i in range(256)]))
            c = float(np.mean([cost(r) for r in rows]))
        else:
            rows, c = eval_lut(lut)
        base[label] = (rows, c)
        print(f"  {label:38s} cost {c:7.2f}   shadow->blk {np.nanmean([r['shadow_to_black'] for r in rows]):5.1f}%"
              f"   high->wht {np.nanmean([r['highlight_to_white'] for r in rows]):5.1f}%"
              f"   pale chroma {np.nanmean([r['pale_chroma_kept'] for r in rows]):5.1f}")

    # --- grid search over the S-curve --------------------------------------------------------------
    # ⚠️ COARSE-TO-FINE WITH AN EXPLICIT BOUNDARY GUARD. A first run put the optimum at toe 1.20 and
    # shoulder 1.20 — both the maximum values searched. A boundary result is a request to widen, never
    # an answer; it is the same trap ADR-084 records and that eink_wpfit already guards against, and
    # it is how a grid quietly reports its own edge as an optimum.
    def search(p_rng, t_rng, s_rng):
        best = None
        for pivot in p_rng:
            for toe in t_rng:
                for sh in s_rng:
                    rows, c = eval_lut(scurve_lut(float(pivot), float(toe), float(sh)))
                    if best is None or c < best[0]:
                        best = (c, float(pivot), float(toe), float(sh), rows)
        return best

    t_hi, s_hi, widened = 2.0, 2.0, 0
    while True:
        best = search(np.arange(0.25, 0.71, 0.05),
                      np.arange(0.0, t_hi + 0.01, t_hi / 8),
                      np.arange(0.0, s_hi + 0.01, s_hi / 8))
        at_edge = (abs(best[2] - t_hi) < 1e-6) or (abs(best[3] - s_hi) < 1e-6)
        if not at_edge or widened >= 3:
            if at_edge:
                print(f"  ⚠️ STILL AT THE GRID EDGE after {widened} widenings — reported, not trusted")
            break
        widened += 1
        t_hi, s_hi = t_hi * 1.8, s_hi * 1.8
        print(f"  optimum on the boundary — widening to toe/shoulder <= {t_hi:.1f}")
    c, pivot, toe, shoulder, rows = best
    print(f"  (searched toe/shoulder to {t_hi:.1f}, widened {widened}x)")
    print(f"\n  BEST S-CURVE  pivot {pivot:.2f}  toe {toe:.2f}  shoulder {shoulder:.2f}   cost {c:7.2f}")
    print(f"    shadow->blk {np.nanmean([r['shadow_to_black'] for r in rows]):5.1f}%"
          f"   high->wht {np.nanmean([r['highlight_to_white'] for r in rows]):5.1f}%"
          f"   pale chroma {np.nanmean([r['pale_chroma_kept'] for r in rows]):5.1f}"
          f"   true black {np.mean([r['true_black_pct'] for r in rows]):5.1f}%")

    print("\n  per work:")
    print(f"    {'work':46s} {'shadow->blk':>12} {'high->wht':>10} {'pale chroma':>12}")
    for (name, _), r in zip(works, rows):
        print(f"    {name:46s} {r['shadow_to_black']:11.1f}% {r['highlight_to_white']:9.1f}%"
              f" {r['pale_chroma_kept']:12.1f}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ RECORD THE CONDITIONS, NOT JUST THE RESULT. The 2026-08-29 fit could not be reproduced from
    # this file: its metrics only reappear at --max-px 380, which the file did not record. Same
    # signature as the eleven instrument defects — a record that omits what it was measured under.
    json.dump({"pivot": pivot, "toe": toe, "shoulder": shoulder, "cost": c,
               "max_px": args.max_px, "images": [str(Path(p)) for p in args.images],
               "weights": {"w_shadow": 1.0, "w_high": 1.0, "w_chroma": 1.0, "w_grain": 0.35,
                           "w_tone": 3.0, "w_contrast": 40.0},
               "lut": scurve_lut(pivot, toe, shoulder),
               "baselines": {k: v[1] for k, v in base.items()},
               "per_work": {n: r for (n, _), r in zip(works, rows)}},
              open(args.out, "w"), indent=1)
    print(f"\n  -> {args.out}")


if __name__ == "__main__":
    main()
