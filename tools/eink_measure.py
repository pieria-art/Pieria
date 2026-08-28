"""
tools/eink_measure.py — turn a photograph of the panel into measurements
(maintainer tool — NOT part of the runtime image).

The companion to `eink_target.py`. That module renders a frame carrying its own calibration
furniture; this one reads a photograph of it back and recovers what the panel actually produced.

    render -> push to panel -> photograph -> rectify -> normalise -> measure

WHY EACH STEP EXISTS

  * RECTIFY. The black registration frame is the outermost dark structure in the shot by
    construction (a white gutter separates it from the bezel). Its four extreme corners map to
    known render coordinates, which gives a homography — so the photo is resampled back onto the
    render's own pixel grid and can be compared like-for-like. Even on a fixed overhead rig this is
    re-solved per frame: a mount that gets nudged would otherwise corrupt every later measurement
    with no visible symptom.

  * NORMALISE. Room light, viewing angle and the camera's auto white balance distort the capture and
    CHANGE BETWEEN SHOTS, so a camera offset measured once would be wrong by the next photograph.
    The pure-ink patch strip is in every frame, so a per-channel affine correction is solved from
    the same photograph it corrects. Shots taken minutes apart under different light stay comparable.

  * MEASURE. Dithered output must be FUSED (downscaled) before comparison: Floyd-Steinberg carries
    colour in the spatial mix of pure primaries, so per-pixel comparison of a dithered frame is
    meaningless — an early attempt at this measured every pixel as fully saturated and told us
    nothing.

WHAT THIS IS NOT. Absolute colorimetry. A phone or webcam under room light cannot deliver that, and
we do not need it: the job is RANKING recipes against the same reference, and a relative comparison
survives a great deal of camera error once both frames are anchored to the same patches.

SELF-TEST. `selftest` synthesises photographs — known perspective warp, known colour distortion,
noise — and checks the pipeline recovers the known truth. Built that way deliberately, so the code
was validated before any hardware existed rather than debugged against a panel.

    python -m tools.eink_measure selftest
    python -m tools.eink_measure capture --device /dev/video0 --out shot.png
    python -m tools.eink_measure read shot.png --target bench-eink/target_huegrid_1600x1200.png
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import epaper as ep  # noqa: E402
from tools import eink_target as et  # noqa: E402

DARK_MAX = 90          # a pixel this dark, after normalising the frame's own range, is "frame"


# --- geometry ------------------------------------------------------------------------------------

def find_frame_corners(img: Image.Image, roi=None) -> list:
    """Four corners of the outermost dark quadrilateral, as (x, y) in photo pixels.

    Uses extremes of x+y and x-y rather than contour following: the registration frame is the
    outermost dark structure, so its corners are the extreme points of the dark mask along both
    diagonals. No OpenCV, no connected components, and it degrades predictably.

    ⚠️ The panel BEZEL and the surround are dark too, and the first version of this grabbed those
    instead of the frame — the self-test caught it before any hardware existed. So the panel is
    located FIRST (bright content's bounding box, since the render's outermost pixels are a white
    gutter) and the frame is sought inside that. `roi` remains available to override when a scene
    has other bright objects in it.
    """
    a = np.asarray(img.convert("L")).astype(float)
    if roi:
        x0, y0, x1, y1 = roi
        sub = a[y0:y1, x0:x1]
        off = (x0, y0)
    else:
        sub = a
        off = (0, 0)
    lo, hi = float(sub.min()), float(sub.max())
    norm = (sub - lo) / max(hi - lo, 1e-6) * 255.0

    # FIND THE PANEL FIRST. The bezel and the surround are dark too, and a naive dark-extremes
    # search grabs those instead of the registration frame — the self-test caught exactly that.
    # The render's outermost pixels are the white gutter, so the bright content's bounding box IS
    # the panel's active area; the frame is then the outermost dark structure INSIDE it.
    bys, bxs = np.nonzero(norm >= 160.0)
    if len(bxs) >= 100:
        pad = 2
        bx0, bx1 = max(0, int(bxs.min()) - pad), min(norm.shape[1], int(bxs.max()) + 1 + pad)
        by0, by1 = max(0, int(bys.min()) - pad), min(norm.shape[0], int(bys.max()) + 1 + pad)
        mask = np.zeros_like(norm, dtype=bool)
        mask[by0:by1, bx0:bx1] = True
    else:
        mask = np.ones_like(norm, dtype=bool)
    ys, xs = np.nonzero((norm <= DARK_MAX) & mask)
    if len(xs) < 100:
        raise ValueError("no dark registration frame found — check the ROI and the exposure")
    s, d = xs + ys, xs - ys
    idx = {"tl": int(np.argmin(s)), "br": int(np.argmax(s)),
           "tr": int(np.argmax(d)), "bl": int(np.argmin(d))}
    return [(float(xs[idx[k]] + off[0]), float(ys[idx[k]] + off[1])) for k in ("tl", "tr", "br", "bl")]


def _perspective_coeffs(dst_corners, src_corners) -> tuple:
    """Solve the 8 coefficients PIL needs: it maps DEST pixels back into the SOURCE image."""
    A, b = [], []
    for (dx, dy), (sx, sy) in zip(dst_corners, src_corners):
        A.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy]); b.append(sx)
        A.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy]); b.append(sy)
    res = np.linalg.solve(np.asarray(A, dtype=float), np.asarray(b, dtype=float))
    return tuple(res.tolist())


def rectify(photo: Image.Image, w: int, h: int, roi=None) -> Image.Image:
    """Resample the photo onto the render's pixel grid using the registration frame."""
    src = find_frame_corners(photo, roi)
    m = et.OUTER_MARGIN
    dst = [(m, m), (w - 1 - m, m), (w - 1 - m, h - 1 - m), (m, h - 1 - m)]
    coeffs = _perspective_coeffs(dst, src)
    return photo.convert("RGB").transform((w, h), Image.PERSPECTIVE, coeffs, Image.BICUBIC)


# --- photometry ----------------------------------------------------------------------------------

def patch_rects(w: int, h: int, count: int) -> list:
    x0, y0, x1, y1 = et.content_box(w, h)
    sy0 = y1 + et.PATCH_GAP
    total = x1 - x0
    pw = (total - et.PATCH_GAP * (count - 1)) / count
    out = []
    for i in range(count):
        px0 = int(round(x0 + i * (pw + et.PATCH_GAP)))
        px1 = int(round(px0 + pw))
        out.append((px0, sy0, px1, sy0 + et.PATCH_H))
    return out


def _mean_rgb(img: Image.Image, rect, inset: float = 0.22) -> np.ndarray:
    """Mean of a patch's INTERIOR. The inset avoids edge bleed from resampling and from the
    specular highlight that room light puts somewhere on the glass."""
    x0, y0, x1, y1 = rect
    dx, dy = int((x1 - x0) * inset), int((y1 - y0) * inset)
    a = np.asarray(img.crop((x0 + dx, y0 + dy, x1 - dx, y1 - dy)).convert("RGB")).astype(float)
    return a.reshape(-1, 3).mean(axis=0)


def solve_correction(rectified: Image.Image, w: int, h: int) -> tuple:
    """Per-channel affine (gain, offset) mapping photographed patches -> intended ink RGB.

    Deliberately affine per channel rather than a full 3x3: with six patches a 3x3 would start
    fitting the panel's own crosstalk as if it were camera error, and the panel is the thing being
    measured. Gain and offset absorb exposure and white balance, which is what actually varies.
    """
    inks = [tuple(c) for c in ep.SPECTRA6_OUTPUT_PALETTE]
    rects = patch_rects(w, h, len(inks))
    meas = np.stack([_mean_rgb(rectified, r) for r in rects])
    want = np.asarray(inks, dtype=float)
    gain, off = np.zeros(3), np.zeros(3)
    for c in range(3):
        A = np.stack([meas[:, c], np.ones(len(meas))], axis=1)
        sol, *_ = np.linalg.lstsq(A, want[:, c], rcond=None)
        gain[c], off[c] = sol
    resid = float(np.abs((meas * gain + off) - want).mean())
    return gain, off, resid


def apply_correction(img: Image.Image, gain, off) -> Image.Image:
    a = np.asarray(img.convert("RGB")).astype(float) * gain + off
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


# --- measurement ---------------------------------------------------------------------------------

def fuse(img: Image.Image, factor: int = 8) -> np.ndarray:
    return np.asarray(img.resize((max(1, img.width // factor), max(1, img.height // factor)),
                                 Image.BOX)).astype(float)


def read_panel(photo: Image.Image, w: int, h: int, roi=None) -> dict:
    rect = rectify(photo, w, h, roi)
    gain, off, resid = solve_correction(rect, w, h)
    corrected = apply_correction(rect, gain, off)
    return {"rectified": rect, "corrected": corrected,
            "gain": gain.tolist(), "offset": off.tolist(), "patch_residual": resid}


def measured_primaries(corrected: Image.Image, w: int, h: int) -> dict:
    """What the panel ACTUALLY produced for each pure ink, in the corrected photo's terms.

    The point of the `primaries` target: `epaper.SPECTRA6_DITHER_PALETTE` is Pimoroni's measurement
    of a different EL133UF1, and every distance calculation in the renderer assumes it.
    """
    x0, y0, x1, y1 = et.content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    out = {}
    for i, name in enumerate(et.INK_NAMES):
        cx, cy = i % 3, i // 3
        rx0 = x0 + cx * cw // 3
        ry0 = y0 + cy * ch // 2
        rect = (rx0, ry0, x0 + (cx + 1) * cw // 3, y0 + (cy + 1) * ch // 2)
        out[name] = [round(v, 1) for v in _mean_rgb(corrected, rect, inset=0.30)]
    return out


# --- self-test -----------------------------------------------------------------------------------

def _synthesise_photo(target: Image.Image, warp: float, gain, off, noise: float,
                      seed: int) -> Image.Image:
    """Fake a photograph: perspective, camera colour distortion, sensor noise, dark surround."""
    rng = np.random.default_rng(seed)
    w, h = target.size
    pad = int(max(w, h) * 0.12)
    canvas = Image.new("RGB", (w + 2 * pad, h + 2 * pad), (18, 18, 20))   # dark bezel/surround
    canvas.paste(target, (pad, pad))
    W, H = canvas.size
    j = warp * min(W, H)
    src = [(0, 0), (W - 1, 0), (W - 1, H - 1), (0, H - 1)]
    dst = [(rng.uniform(0, j), rng.uniform(0, j)),
           (W - 1 - rng.uniform(0, j), rng.uniform(0, j)),
           (W - 1 - rng.uniform(0, j), H - 1 - rng.uniform(0, j)),
           (rng.uniform(0, j), H - 1 - rng.uniform(0, j))]
    warped = canvas.transform((W, H), Image.PERSPECTIVE, _perspective_coeffs(dst, src), Image.BICUBIC)
    a = np.asarray(warped).astype(float)
    a = a * np.asarray(gain) + np.asarray(off)
    a += rng.normal(0, noise, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


def cmd_selftest(args) -> None:
    w, h = 1600, 1200
    target = et.compose(et.target_primaries(w, h), w, h)
    print("SELF-TEST — synthesise photographs with KNOWN distortion, check recovery\n")
    ok = True
    for i, (warp, gain, off, noise) in enumerate([
            (0.000, (1.00, 1.00, 1.00), (0, 0, 0), 0.0),
            (0.010, (0.92, 1.00, 1.12), (10, 4, -6), 2.0),
            (0.025, (0.80, 0.95, 1.25), (24, 8, -14), 4.0),
            (0.040, (0.70, 0.88, 1.35), (30, 12, -20), 6.0)], 1):
        photo = _synthesise_photo(target, warp, gain, off, noise, seed=i)
        pad = int(max(w, h) * 0.12)
        roi = (pad // 2, pad // 2, photo.width - pad // 2, photo.height - pad // 2)
        try:
            r = read_panel(photo, w, h, roi=roi)
        except Exception as exc:
            print(f"  case {i}: FAILED to read — {exc}"); ok = False; continue
        prim = measured_primaries(r["corrected"], w, h)
        truth = {n: tuple(c) for n, c in zip(et.INK_NAMES, ep.SPECTRA6_OUTPUT_PALETTE)}
        err = max(max(abs(a - b) for a, b in zip(prim[n], truth[n])) for n in truth)
        verdict = "OK" if err <= 12 else "TOO HIGH"
        if err > 12:
            ok = False
        print(f"  case {i}: warp {warp:.3f}  camera gain {gain}  noise {noise:.0f}")
        print(f"           patch residual {r['patch_residual']:5.1f}   worst ink error {err:5.1f}  {verdict}")
    print("\nself-test PASSED" if ok else "\nself-test FAILED")
    if not ok:
        sys.exit(1)


def cmd_capture(args) -> None:
    """Grab one frame with ffmpeg. Discards the first frames so auto-exposure can settle."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2",
           "-video_size", args.size, "-i", args.device, "-frames:v", "1", "-y", args.out]
    if args.warmup > 0:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2",
               "-video_size", args.size, "-i", args.device,
               "-vf", f"select=gte(n\\,{args.warmup})", "-frames:v", "1", "-y", args.out]
    subprocess.run(cmd, check=True)
    print(f"captured -> {args.out}")


def cmd_read(args) -> None:
    photo = Image.open(args.photo)
    w, h = (Image.open(args.target).size if args.target else (args.width, args.height))
    roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else None
    r = read_panel(photo, w, h, roi=roi)
    print(f"gain   {[round(v, 4) for v in r['gain']]}")
    print(f"offset {[round(v, 2) for v in r['offset']]}")
    print(f"patch residual (mean abs, 0-255): {r['patch_residual']:.2f}")
    if r["patch_residual"] > 18:
        print("  ⚠️ high — check for a specular highlight on the glass, or a clipped exposure")
    out = Path(args.out or "bench-eink/measured_corrected.png")
    r["corrected"].save(out)
    print(f"corrected -> {out}")
    if args.primaries:
        print("\nMEASURED PANEL PRIMARIES vs the assumed SPECTRA6_DITHER_PALETTE:")
        prim = measured_primaries(r["corrected"], w, h)
        for name, assumed in zip(et.INK_NAMES, ep.SPECTRA6_DITHER_PALETTE):
            got = prim[name]
            print(f"  {name:7s} measured {got}   assumed {list(assumed)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="validate the pipeline against synthetic photographs")
    c = sub.add_parser("capture", help="grab one frame from a v4l2 device")
    c.add_argument("--device", default="/dev/video0")
    c.add_argument("--size", default="1920x1080")
    c.add_argument("--warmup", type=int, default=12, help="frames to discard so AE/AWB settle")
    c.add_argument("--out", default="bench-eink/shot.png")
    r = sub.add_parser("read", help="rectify + normalise a photograph and report")
    r.add_argument("photo")
    r.add_argument("--target", default="", help="the rendered target, to take w/h from")
    r.add_argument("--width", type=int, default=1600)
    r.add_argument("--height", type=int, default=1200)
    r.add_argument("--roi", default="", help="x0,y0,x1,y1 crop to the panel's active area")
    r.add_argument("--primaries", action="store_true", help="report measured ink primaries")
    r.add_argument("--out", default="")
    args = ap.parse_args()
    {"selftest": cmd_selftest, "capture": cmd_capture, "read": cmd_read}[args.cmd](args)


if __name__ == "__main__":
    main()
