"""
tools/eink_ambient_check.py — is the room dark enough to shoot? A number on the bench, not a judgement.
(maintainer tool — NOT part of the runtime image.)

WHY THIS EXISTS. ADR-112 found that with the lamp off, in an afternoon room, the panel region still
read 410 ADU — 40% of its lamp-on signal — because the panel is the glossiest thing in the rig and
mirrors the room into the lens. That breaks the method, not merely the noise: the per-frame
ColorChecker absorbs a change in overall brightness, but the FLAT FIELD is shot once and is only
valid while the *shape* of the light holds, and daylight through a window changes shape as clouds
move. ADR-114 re-opens a daytime session on one condition: the ambient fraction is MEASURED first
and the shoot is gated on it. This tool is that gate.

WHAT IT MEASURES. Two frames at the frozen rig settings (M · f/5.6 · ISO 100 · 20 s), camera and
rig untouched between them:
  * `--lit`   the lamp ON, panel showing WHITE (the flat-field frame is ideal). Used to locate the
              panel and to give the denominator.
  * `--dark`  the lamp OFF, everything else exactly as during a session — curtains, canopy, laptop
              screen, Pi running. One or more; several across a session measure DRIFT.
Reports the panel-region median above black for each, scales them to a common exposure, and prints
the ambient fraction with a verdict:
  * ≤ GO (3%)        shoot. A 2x swing in cloud cover moves the flat field's shape by ~3%, inside the
                     26/255 tonefine error bar (ADR-108 / error_bars.json).
  * ≤ BRACKET (10%)  shoot, but bracket: flat field at start AND end, ambient frame every ~30 min,
                     and expect some grid-mean findings (3.3/255) to be inconclusive.
  * > BRACKET        do not shoot. Bank the rebuild; wait for dark.
The thresholds are arguments, not gospel — they are set from the error bars, not from a standard.

WHY A BLOCK MAP AND NOT ONE NUMBER. The 09-04 frame was diagnosed by its *shape*: dark at the flock
edges, up to 647 ADU across the panel's middle. A single median cannot tell "the room is bright"
from "one window is mirrored in the top-left corner", and only the second is a canopy problem.

⚠️ A FRAME THAT CANNOT FAIL PROVES NOTHING (ADR-112). A lens-cap frame and a genuinely dark room are
pixel-identical — DSC00270 was read as "ambient is zero" and was a lens cap. This tool flags a dark
frame whose block map is flat and within a few ADU of zero as *indistinguishable from cap-on* and
refuses to call it GO until you confirm the cap was off (`--cap-was-off`).

`compare` is the bracket check: two lit frames of the same target (the start and end flat fields)
→ the per-block ratio B/A over the panel, and the worst deviation from the median ratio. That
deviation IS the flat-field shape drift over the session, in the same % as the gate.

    python -m tools.eink_ambient_check check --lit DSC00268.ARW --dark DSC00271.ARW
    python -m tools.eink_ambient_check check --lit flat.ARW --dark amb1.ARW amb2.ARW amb3.ARW
    python -m tools.eink_ambient_check compare --a flat_start.ARW --b flat_end.ARW
    python -m tools.eink_ambient_check selftest
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GO_PCT = 3.0            # ≤ this: shoot
BRACKET_PCT = 10.0      # ≤ this: shoot with start/end flats + periodic ambient frames
CAP_ON_ADU = 4.0        # a dark frame whose every block is within this of zero looks like a lens cap
DEFAULT_GRID = (6, 8)   # rows x cols of the block map over the panel ROI


# --- pure-array logic — no file I/O, fully unit-testable without a sample .ARW --------------------

def block_map(plane: np.ndarray, roi: tuple, grid: tuple = DEFAULT_GRID) -> np.ndarray:
    """Median of `plane` over a rows x cols grid of blocks spanning `roi` = (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = roi
    rows, cols = grid
    sub = plane[y0:y1, x0:x1]
    h, w = sub.shape
    if h < rows or w < cols:
        raise ValueError(f"ROI {w}x{h} is smaller than the {cols}x{rows} block grid")
    ys = np.linspace(0, h, rows + 1).astype(int)
    xs = np.linspace(0, w, cols + 1).astype(int)
    out = np.empty((rows, cols), dtype=np.float64)
    for r in range(rows):
        for c in range(cols):
            out[r, c] = np.median(sub[ys[r]:ys[r + 1], xs[c]:xs[c + 1]])
    return out


def trim_roi_to_lit(plane: np.ndarray, roi: tuple, grid: tuple = DEFAULT_GRID,
                    floor: float = 0.3) -> tuple:
    """Shrink `roi` until every edge row/column of its block map reads ≥ `floor` x the ROI median.

    `panel_bbox` seeds from bright-and-neutral pixels and is happy to over-reach into the tray or
    the flock beside the panel (on DSC00268 it took two block rows of 150-300 ADU under a panel
    reading 2000-3400). Those rows are not panel; averaging them in understates both the lamp-on
    signal and the ambient. Trimming on the LIT frame keeps the ROI honest for the dark one, which
    is measured over the identical rectangle.
    """
    x0, y0, x1, y1 = roi
    rows, cols = grid
    for _ in range(rows + cols):
        bm = block_map(plane, (x0, y0, x1, y1), grid)
        cut = floor * float(np.median(bm))
        bh, bw = (y1 - y0) / rows, (x1 - x0) / cols
        if np.median(bm[0]) < cut:
            y0 = int(round(y0 + bh))
        elif np.median(bm[-1]) < cut:
            y1 = int(round(y1 - bh))
        elif np.median(bm[:, 0]) < cut:
            x0 = int(round(x0 + bw))
        elif np.median(bm[:, -1]) < cut:
            x1 = int(round(x1 - bw))
        else:
            break
    return (x0, y0, x1, y1)


def ambient_fraction(dark_adu: float, lit_adu: float, dark_exposure: float, lit_exposure: float) -> float:
    """Ambient as a fraction of lamp-on signal, both scaled to the LIT frame's exposure.

    Exposure scaling is linear because the frames are scene-linear sensor counts above black. It is
    a convenience for a mismatched pair — shoot both at the frozen 20 s and the factor is 1.
    """
    if lit_adu <= 0:
        raise ValueError("lit frame reads zero over the panel — wrong ROI, or the lamp was off")
    if dark_exposure <= 0 or lit_exposure <= 0:
        raise ValueError("exposure times must be positive")
    return (dark_adu * (lit_exposure / dark_exposure)) / lit_adu


def verdict(fraction: float, go: float = GO_PCT, bracket: float = BRACKET_PCT) -> str:
    pct = fraction * 100.0
    if pct <= go:
        return "GO"
    if pct <= bracket:
        return "BRACKET"
    return "NO-GO"


def looks_like_cap_on(dark_blocks: np.ndarray, floor: float = CAP_ON_ADU) -> bool:
    """True when the dark frame's block map is flat and at the noise floor — the DSC00270 shape.

    A genuinely dark room can produce the same map, which is the point: the tool cannot tell them
    apart, so it must not silently say GO on such a frame.
    """
    return bool(np.all(np.abs(dark_blocks) <= floor))


def shape_drift(blocks_a: np.ndarray, blocks_b: np.ndarray) -> tuple[np.ndarray, float]:
    """Per-block ratio B/A and the worst |deviation| from the median ratio, as a fraction.

    A uniform brightness change gives ratio == median everywhere and drift 0 — the ColorChecker
    absorbs that. What survives here is the part the flat field cannot absorb.
    """
    if blocks_a.shape != blocks_b.shape:
        raise ValueError("block maps differ in shape")
    if np.any(blocks_a <= 0):
        raise ValueError("frame A has a non-positive block — wrong ROI, or the lamp was off")
    ratio = blocks_b / blocks_a
    med = float(np.median(ratio))
    return ratio, float(np.max(np.abs(ratio / med - 1.0)))


# --- file-backed helpers -------------------------------------------------------------------------

def _load(path):
    from tools import eink_raw
    return eink_raw.decode(path)


def _adu_plane(frame) -> np.ndarray:
    """Green-channel counts above black. G is the sensor's best-sampled channel (both CFA greens)."""
    return frame.rgb[..., 1] * (frame.saturation - frame.black)


def _locate_panel(frame, roi=None) -> tuple:
    """Panel ROI from the LIT frame via eink_measure.panel_bbox on an 8-bit rendering."""
    if roi is not None:
        return roi
    from PIL import Image

    from tools.eink_measure import panel_bbox
    rgb = frame.rgb
    ref = np.percentile(rgb.reshape(-1, 3), 99.5, axis=0)
    img8 = np.clip(rgb / np.maximum(ref, 1e-9) * 255.0, 0, 255).astype(np.uint8)
    seed = panel_bbox(Image.fromarray(img8, "RGB"))
    return trim_roi_to_lit(_adu_plane(frame), seed)


def _parse_roi(s: str | None):
    if not s:
        return None
    parts = [int(v) for v in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--roi wants x0,y0,x1,y1 in binned-frame pixels")
    return tuple(parts)


def _print_blocks(title: str, blocks: np.ndarray, fmt: str = "{:7.0f}") -> None:
    print(f"  {title}")
    for row in blocks:
        print("    " + " ".join(fmt.format(v) for v in row))


# --- commands ------------------------------------------------------------------------------------

def cmd_check(args) -> int:
    lit = _load(args.lit)
    roi = _locate_panel(lit, _parse_roi(args.roi))
    lit_plane = _adu_plane(lit)
    lit_blocks = block_map(lit_plane, roi, args.grid)
    lit_med = float(np.median(lit_plane[roi[1]:roi[3], roi[0]:roi[2]]))
    lit_exp = float(lit.meta["exposure_time"])
    print(f"lit   {Path(args.lit).name}  {lit_exp:g}s f/{lit.meta['f_number']:g} ISO {lit.meta['iso']:g}  "
          f"panel ROI {roi}  median {lit_med:.0f} ADU  clipped {lit.clipped_fraction * 100:.3f}%")
    _print_blocks("lamp-on block map (ADU above black)", lit_blocks)

    worst = "GO"
    order = {"GO": 0, "BRACKET": 1, "NO-GO": 2}
    first_blocks = None
    for p in args.dark:
        dk = _load(p)
        dk_plane = _adu_plane(dk)
        dk_blocks = block_map(dk_plane, roi, args.grid)
        dk_med = float(np.median(dk_plane[roi[1]:roi[3], roi[0]:roi[2]]))
        dk_exp = float(dk.meta["exposure_time"])
        frac = ambient_fraction(dk_med, lit_med, dk_exp, lit_exp)
        v = verdict(frac, args.go, args.bracket)
        cap = looks_like_cap_on(dk_blocks)
        print(f"\ndark  {Path(p).name}  {dk_exp:g}s  panel median {dk_med:.0f} ADU"
              f"{'' if dk_exp == lit_exp else f' (x{lit_exp / dk_exp:.3g} to lit exposure)'}"
              f"  → ambient {frac * 100:.1f}% of lamp-on   {v}")
        _print_blocks("lamp-off block map (ADU above black) — shape says WHERE the room is mirrored",
                      dk_blocks)
        if cap and not args.cap_was_off:
            print(f"  ⚠️ flat and within ±{CAP_ON_ADU:.0f} ADU of zero everywhere — INDISTINGUISHABLE FROM A "
                  "LENS-CAP FRAME (DSC00270, ADR-112). Not calling GO. Confirm the cap was off and re-run "
                  "with --cap-was-off, or re-shoot with the room light briefly on to prove the frame can see.")
            v = "UNPROVEN"
        if first_blocks is None:
            first_blocks = dk_blocks
        else:
            delta = dk_blocks - first_blocks
            print(f"  drift vs first dark frame: {delta.min():+.0f} .. {delta.max():+.0f} ADU per block "
                  f"({(np.abs(delta).max() / lit_med) * 100:.1f}% of lamp-on)")
        if v == "UNPROVEN":
            worst = "UNPROVEN"
        elif worst != "UNPROVEN" and order[v] > order[worst]:
            worst = v

    print(f"\nverdict: {worst}   (GO ≤ {args.go:g}%, BRACKET ≤ {args.bracket:g}%, else NO-GO)")
    if worst == "BRACKET":
        print("  shoot, and bracket: flat field at START and END (check with `compare`), an ambient frame "
              "every ~30 min, and treat grid-mean findings (3.3/255) as provisional.")
    elif worst == "NO-GO":
        print("  do not shoot. The panel mirrors what is in the camera's mirror direction — with the camera "
              "looking straight down, that is the CEILING around the lens, ~2x the panel's size at camera "
              "height. Canopy that, then re-measure.")
    return 0 if worst == "GO" else 1


def cmd_compare(args) -> int:
    a, b = _load(args.a), _load(args.b)
    roi = _locate_panel(a, _parse_roi(args.roi))
    ba, bb = block_map(_adu_plane(a), roi, args.grid), block_map(_adu_plane(b), roi, args.grid)
    ratio, drift = shape_drift(ba, bb)
    med = float(np.median(ratio))
    print(f"a {Path(args.a).name}  b {Path(args.b).name}  panel ROI {roi}")
    print(f"overall brightness b/a = {med:.4f} (the ColorChecker absorbs this)")
    _print_blocks("per-block ratio b/a", ratio, fmt="{:7.4f}")
    print(f"shape drift (worst |block/median - 1|) = {drift * 100:.2f}%   "
          f"{'OK' if drift * 100 <= args.go else 'EXCEEDS the gate — frames between a and b are suspect'}")
    return 0 if drift * 100 <= args.go else 1


def cmd_selftest(args) -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok &= bool(cond)

    plane = np.full((120, 160), 100.0)
    plane[:, 80:] = 300.0
    bm = block_map(plane, (0, 0, 160, 120), (2, 2))
    check("block_map splits a two-level plane", np.allclose(bm, [[100, 300], [100, 300]]))
    plane2 = np.full((120, 160), 1000.0)
    plane2[100:, :] = 50.0                              # a dark tray strip under the panel
    check("trim_roi_to_lit drops the dark bottom row",
          trim_roi_to_lit(plane2, (0, 0, 160, 120), (6, 8)) == (0, 0, 160, 100))
    check("ambient_fraction scales exposure", abs(ambient_fraction(100, 1000, 10, 20) - 0.2) < 1e-12)
    check("verdict thresholds", (verdict(0.03), verdict(0.031), verdict(0.10), verdict(0.11))
          == ("GO", "BRACKET", "BRACKET", "NO-GO"))
    check("cap-on detector fires on a flat ±2 ADU map", looks_like_cap_on(np.full((6, 8), 2.0)))
    check("cap-on detector stays quiet on a 410 ADU map", not looks_like_cap_on(np.full((6, 8), 410.0)))
    a = np.full((6, 8), 1000.0)
    _, d0 = shape_drift(a, a * 1.4)
    check("uniform x1.4 change is zero shape drift", d0 < 1e-12)
    b = a.copy()
    b[0, 0] *= 1.05
    _, d1 = shape_drift(a, b)
    check("one block +5% is ~5% shape drift", abs(d1 - 0.05) < 1e-9)
    print("\nself-test PASSED" if ok else "\nself-test FAILED")
    return 0 if ok else 1


def _grid(s: str) -> tuple:
    r, c = s.lower().split("x")
    return int(r), int(c)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ck = sub.add_parser("check", help="ambient fraction of lamp-on signal over the panel, with verdict")
    ck.add_argument("--lit", required=True, help="lamp ON, panel showing white (the flat-field frame)")
    ck.add_argument("--dark", required=True, nargs="+", help="lamp OFF frame(s), rig untouched")
    ck.add_argument("--roi", help="x0,y0,x1,y1 in binned-frame pixels (default: auto from --lit)")
    ck.add_argument("--grid", type=_grid, default=DEFAULT_GRID, help="block map rows x cols (6x8)")
    ck.add_argument("--go", type=float, default=GO_PCT)
    ck.add_argument("--bracket", type=float, default=BRACKET_PCT)
    ck.add_argument("--cap-was-off", action="store_true", help="assert the flat dark frame is real")
    cp = sub.add_parser("compare", help="flat-field shape drift between two lit frames (start vs end)")
    cp.add_argument("--a", required=True)
    cp.add_argument("--b", required=True)
    cp.add_argument("--roi")
    cp.add_argument("--grid", type=_grid, default=DEFAULT_GRID)
    cp.add_argument("--go", type=float, default=GO_PCT, help="drift %% that still passes the gate")
    sub.add_parser("selftest", help="validate the pure-array logic against known-constructed data")
    args = ap.parse_args()
    sys.exit({"check": cmd_check, "compare": cmd_compare, "selftest": cmd_selftest}[args.cmd](args))


if __name__ == "__main__":
    main()
