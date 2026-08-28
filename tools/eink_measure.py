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
    """Per-channel affine solved from the BLACK and WHITE patches ONLY.

    ⚠️ A first version anchored on all six inks, mapping them to SPECTRA6_OUTPUT_PALETTE — the PURE
    primaries. That is wrong twice over. The panel physically cannot emit pure (255,0,0); it emits a
    muted ink. So the fit was asked to reach impossible targets and responded with extreme gains and
    clipping (measured: residual 65-108/255, a negative channel gain). And it was CIRCULAR: the
    chromatic inks' true values are exactly what we are trying to measure, so they cannot also be the
    anchors.

    Black and white are the only legitimate anchors, because their role is definitional rather than
    measured: they set the tonal range, absorbing exposure and white balance. Everything is then
    expressed in PANEL-RELATIVE units — the panel's own black is 0 and its own white is 255 — which
    is self-consistent, needs no prior knowledge of the inks, and is directly comparable to any other
    palette normalised the same way.
    """
    rects = patch_rects(w, h, len(ep.SPECTRA6_OUTPUT_PALETTE))
    black = _mean_rgb(rectified, rects[0])
    white = _mean_rgb(rectified, rects[1])
    span = np.maximum(white - black, 1e-3)
    gain = 255.0 / span
    off = -black * gain
    # Diagnostic: how uniform is each patch? A specular highlight or uneven light shows up here long
    # before it shows up as a wrong colour, and silently biases every later measurement.
    stds = []
    for r in rects:
        x0, y0, x1, y1 = r
        dx, dy = int((x1 - x0) * 0.22), int((y1 - y0) * 0.22)
        a = np.asarray(rectified.crop((x0 + dx, y0 + dy, x1 - dx, y1 - dy))).astype(float)
        stds.append(float(a.reshape(-1, 3).std(axis=0).mean()))
    return gain, off, float(np.mean(stds))


def normalise_palette(palette) -> list:
    """Express an assumed palette in the same panel-relative units, so it can be compared."""
    arr = np.asarray([list(c) for c in palette], dtype=float)
    black, white = arr[0], arr[1]
    span = np.maximum(white - black, 1e-3)
    return [[round(v, 1) for v in ((c - black) * 255.0 / span)] for c in arr]


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


def _grab(device: str, size: str, warmup: int, out: str) -> None:
    _v4l2_set(device, *GAIN_CTRL)      # see GAIN_CTRL: this camera walks its gain back up on its own
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2",
           "-video_size", size, "-i", device]
    if warmup > 0:
        cmd += ["-vf", f"select=gte(n\\,{warmup})"]
    cmd += ["-frames:v", "1", "-y", out]
    subprocess.run(cmd, check=True)


def _frame_delta(a: Path, b: Path) -> float:
    """Mean absolute difference between two frames, 0-255."""
    x = np.asarray(Image.open(a).convert("L").resize((320, 180), Image.BOX)).astype(float)
    y = np.asarray(Image.open(b).convert("L").resize((320, 180), Image.BOX)).astype(float)
    return float(np.abs(x - y).mean())


#: Locked C920 settings for the overhead rig, found by sweeping against the panel 2026-08-28.
#: exposure 200 + gain 24 puts the panel's white at 253 with p99.9 = 247 and ZERO clipped pixels —
#: nearly the full range with nothing lost at the top. A clipped white anchor invalidates the whole
#: correction, so headroom matters more than brightness here.
CAMERA_LOCK = [
    ("auto_exposure", 1),                 # 1 = Manual Mode
    ("exposure_time_absolute", 200),
    ("white_balance_automatic", 0),
    ("white_balance_temperature", 4000),
    ("exposure_dynamic_framerate", 0),
    ("backlight_compensation", 0),
    ("power_line_frequency", 2),          # 60 Hz — stops mains flicker beating with the shutter
    ("gain", 24),
]
#: ⚠️ The C920 drives GAIN back up on its own (measured: 0 -> 109 -> 255 as exposure rose) even in
#: manual exposure mode. Every capture therefore re-asserts it immediately before grabbing. Without
#: this the panel clips ~30% of its pixels and every measurement built on it is silently wrong.
GAIN_CTRL = ("gain", 24)


def _v4l2_set(device: str, ctrl: str, value) -> None:
    subprocess.run(["v4l2-ctl", "-d", device, f"--set-ctrl={ctrl}={value}"],
                   check=False, capture_output=True)


def _v4l2_get(device: str, ctrl: str):
    r = subprocess.run(["v4l2-ctl", "-d", device, f"--get-ctrl={ctrl}"],
                       check=False, capture_output=True, text=True)
    try:
        # menu controls read back as "auto_exposure: 1 (Manual Mode)" — take the leading integer
        return int(r.stdout.strip().split(":")[1].strip().split()[0])
    except (IndexError, ValueError):
        return None


def cmd_lock(args) -> None:
    """Pin the camera so nothing drifts between measurements, then VERIFY it took.

    Auto exposure, auto white balance and autofocus each re-decide per frame, so with them on, two
    photographs of the same panel are two different measurements. Focus must be set AFTER disabling
    continuous autofocus — the control is inactive until then and the write fails with 'Permission
    denied', which looks like a permissions problem and is not one.
    """
    dev = args.device
    for ctrl, val in CAMERA_LOCK:
        _v4l2_set(dev, ctrl, val)
    _v4l2_set(dev, "focus_automatic_continuous", 0)
    _v4l2_set(dev, "focus_absolute", args.focus)
    _v4l2_set(dev, *GAIN_CTRL)
    print("locked camera controls (read back):")
    bad = []
    for ctrl, val in CAMERA_LOCK + [("focus_automatic_continuous", 0), ("focus_absolute", args.focus)]:
        got = _v4l2_get(dev, ctrl)
        ok = got == val
        if not ok:
            bad.append((ctrl, val, got))
        print(f"  {ctrl:32s} want {val:5} got {got}  {'ok' if ok else '<-- DID NOT TAKE'}")
    if bad:
        print("\n⚠️ some controls did not take. Read-back is not optional here: this camera "
              "silently overrode gain and exposure during setup, and the only symptom was clipped "
              "measurements that looked like a lighting problem.")


def cmd_capture(args) -> None:
    """Grab a frame, optionally waiting until the PANEL has stopped changing.

    ⚠️ AN E-INK REFRESH IS NOT INSTANT AND IT IS NOT MONOTONIC. A Spectra 6 update takes ~9-16 s and
    drives the pixels through inversion and flashing phases on the way, so a frame grabbed too early
    is not a slightly-early version of the final image — it is a DIFFERENT image. The first real
    capture of this rig was taken mid-refresh and produced a patch residual of 97/255 and a negative
    blue gain, which reads exactly like a badly mis-calibrated camera rather than like a timing bug.

    So settle by OBSERVATION rather than by a fixed sleep: keep grabbing until two consecutive frames
    agree, which adapts to whatever the panel and the lighting are actually doing. A fixed delay
    would be both slower on average and wrong exactly when the panel is slowest.
    """
    if not args.settle:
        _grab(args.device, args.size, args.warmup, args.out)
        print(f"captured -> {args.out}")
        return
    tmp_a = Path(args.out).with_suffix(".settle_a.png")
    tmp_b = Path(args.out).with_suffix(".settle_b.png")
    _grab(args.device, args.size, args.warmup, str(tmp_a))
    for attempt in range(1, args.settle_tries + 1):
        _grab(args.device, args.size, max(2, args.warmup // 4), str(tmp_b))
        delta = _frame_delta(tmp_a, tmp_b)
        print(f"  settle {attempt}: frame delta {delta:6.2f}")
        if delta <= args.settle_delta:
            tmp_b.replace(Path(args.out))
            tmp_a.unlink(missing_ok=True)
            print(f"captured (settled) -> {args.out}")
            return
        tmp_b.replace(tmp_a)
    tmp_a.replace(Path(args.out))
    print(f"⚠️ never settled below {args.settle_delta} in {args.settle_tries} tries — "
          f"kept the last frame. The panel may still be refreshing, or something in shot is moving.")


def cmd_read(args) -> None:
    photo = Image.open(args.photo)
    w, h = (Image.open(args.target).size if args.target else (args.width, args.height))
    roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else None
    r = read_panel(photo, w, h, roi=roi)
    print(f"gain   {[round(v, 4) for v in r['gain']]}")
    print(f"offset {[round(v, 2) for v in r['offset']]}")
    print(f"patch non-uniformity (mean std within patches, 0-255): {r['patch_residual']:.2f}")
    if r["patch_residual"] > 14:
        print("  ⚠️ high — uneven light or a specular highlight on the glass. Both bias every "
              "later measurement, so fix the lighting rather than correcting for it.")
    out = Path(args.out or "bench-eink/measured_corrected.png")
    r["corrected"].save(out)
    print(f"corrected -> {out}")
    if args.primaries:
        print("\nMEASURED PANEL PRIMARIES vs the assumed SPECTRA6_DITHER_PALETTE")
        print("(both in PANEL-RELATIVE units: this panel's own black = 0, its own white = 255)")
        prim = measured_primaries(r["corrected"], w, h)
        assumed_norm = normalise_palette(ep.SPECTRA6_DITHER_PALETTE)
        worst = 0.0
        for name, assumed in zip(et.INK_NAMES, assumed_norm):
            got = prim[name]
            d = max(abs(a - b) for a, b in zip(got, assumed))
            worst = max(worst, d)
            flag = "  <-- DIFFERS" if d > 30 else ""
            print(f"  {name:7s} measured {[round(v) for v in got]}   assumed {[round(v) for v in assumed]}"
                  f"   worst channel {d:5.1f}{flag}")
        print(f"\n  largest disagreement: {worst:.1f}/255")
        if worst > 30:
            print("  This panel does not match Pimoroni's measurement, and every distance "
                  "calculation in the renderer assumes it does.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="validate the pipeline against synthetic photographs")
    lk = sub.add_parser("lock", help="pin camera exposure/WB/focus/gain and verify it took")
    lk.add_argument("--device", default="/dev/video0")
    lk.add_argument("--focus", type=int, default=30)
    c = sub.add_parser("capture", help="grab one frame from a v4l2 device")
    c.add_argument("--device", default="/dev/video0")
    c.add_argument("--size", default="1920x1080")
    c.add_argument("--warmup", type=int, default=12, help="frames to discard so AE/AWB settle")
    c.add_argument("--out", default="bench-eink/shot.png")
    c.add_argument("--settle", action="store_true",
                   help="keep grabbing until two consecutive frames agree — an e-ink refresh passes "
                        "through inversion phases, so an early grab is a different image, not an "
                        "early one")
    c.add_argument("--settle-delta", type=float, default=1.2,
                   help="mean abs frame difference (0-255) counted as settled")
    c.add_argument("--settle-tries", type=int, default=25)
    r = sub.add_parser("read", help="rectify + normalise a photograph and report")
    r.add_argument("photo")
    r.add_argument("--target", default="", help="the rendered target, to take w/h from")
    r.add_argument("--width", type=int, default=1600)
    r.add_argument("--height", type=int, default=1200)
    r.add_argument("--roi", default="", help="x0,y0,x1,y1 crop to the panel's active area")
    r.add_argument("--primaries", action="store_true", help="report measured ink primaries")
    r.add_argument("--out", default="")
    args = ap.parse_args()
    {"selftest": cmd_selftest, "capture": cmd_capture, "read": cmd_read,
     "lock": cmd_lock}[args.cmd](args)


if __name__ == "__main__":
    main()
