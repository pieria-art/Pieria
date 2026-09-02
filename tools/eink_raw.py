"""
tools/eink_raw.py — Sony NEX-6 `.ARW` ingest for the e-ink measurement rig
(maintainer tool — NOT part of the runtime image).

The rig used to photograph the panel with an 8-bit webcam (`eink_measure.py`). This module replaces
that path with a real camera's raw sensor data, which matters because the webcam quantises to 256
levels AFTER its own auto-exposure and gamma have already thrown away the dynamic range this project
needs to measure. A `.ARW` gives 14-bit linear counts straight off the sensor, before any of that.

WHY EACH DESIGN CHOICE EXISTS

  * 2x2 BINNING, NOT DEMOSAICING, IS THE DEFAULT. Averaging each 2x2 CFA block gives clean, fully
    independent R, G, B samples with zero interpolation. Demosaicing infers each channel from its
    NEIGHBOURS, which means a green pixel's estimate is contaminated by whatever red or blue sits next
    to it — exactly the cross-channel error this rig cannot tolerate when measuring a patch mean beside
    a differently-coloured patch. Binned resolution is 2460x1638, which still hugely oversamples a
    1600px panel render, so nothing is lost by giving up the other 3/4 of the pixels. `demosaic=True`
    is offered for the rare case that needs full resolution more than it needs zero contamination.

  * `output_color=rawpy.ColorSpace.raw` IS LOAD-BEARING WHEN DEMOSAICING. Any other value makes LibRaw
    apply ITS OWN camera-to-sRGB colour matrix before handing the pixels back — which is precisely the
    camera-to-panel transform this whole project exists to solve for itself from a photograph. Get this
    wrong and every downstream number is silently measuring LibRaw's colour science, not the panel's.

  * THE THREE-TERM PEDESTAL STAYS SEPARABLE. A raw count is electronic offset + dark current + signal,
    and these must never be conflated into one "black level":
      - electronic offset: a fixed 512-count floor, the same for all four CFA sites. Because it is a
        SENSOR property, not a per-shot one, it is a constant here, not something re-derived from every
        photograph — see measure_black for why deriving it from a single margin would corrupt it.
      - dark current: scales with exposure time and temperature, varies spatially, and produces hot
        pixels. Only a MEDIAN over the light-shielded margin is available here (a scalar, no spatial
        detail); a full spatial map needs a genuine dark frame (`dark_frame=`).
      - optical veiling glare: invisible to a masked-pixel measurement by construction — a light trap
        downstream is the only thing that can see it. This module does not claim to.

  * SATURATION IS OVERRIDDEN TO AN EMPIRICAL VALUE, NOT LIBRAW'S. LibRaw reports `white_level = 16383`
    for this body, but on real frames 77,466 pixels pile up at exactly 16372 with only a 631-pixel tail
    above it — the sensor (or Sony's lossy-compressed ARW encoding) never actually reaches 16383. Using
    LibRaw's number as the clip threshold would call thousands of genuinely saturated pixels "fine".
    The raw data is also quantised in steps of 32 near saturation: this body's ARW is always compressed,
    there is no uncompressed mode to fall back on.

WHAT THIS IS NOT. A general-purpose raw converter. It exists to produce clean, camera-native-primary,
scene-linear samples for a measurement rig — not a pleasing picture.

    python -m tools.eink_raw info bench-eink/camera/nex6-samples-2026-09-01/DSC00238.ARW
    python -m tools.eink_raw selftest
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rawpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- Sony NEX-6 sensor constants, MEASURED 2026-09-01 against
# bench-eink/camera/nex6-samples-2026-09-01/*.ARW. Do not substitute LibRaw defaults for these; that is
# the whole point of measuring them. ---------------------------------------------------------------

RAW_SHAPE = (3276, 4928)              # raw_image, uint16
VISIBLE_SHAPE = (3276, 4920)          # raw_image_visible — 8 masked columns live on the RIGHT ONLY
                                       # (sizes.top_margin == sizes.left_margin == 0)
BLACK_LEVEL = 512.0                   # nominal electronic floor, identical across all 4 CFA sites
SATURATION = 16372.0                  # empirical clip point — see module docstring; NOT LibRaw's 16383
LIBRAW_WHITE_LEVEL = 16383            # what LibRaw itself reports for this body; kept only so `info`
                                       # can flag if it ever stops matching what this module assumes
QUANT_STEP = 32                       # Sony's compressed ARW quantises counts in steps of this size
                                       # near saturation; there is no uncompressed mode on this body
CAMERA_MODEL = "Sony NEX-6"           # rawpy exposes the LENS make/model (raw.lens) but not the camera
                                       # body itself, so this is fixed rather than read from the file

#: Mandatory `postprocess()` flags for `demosaic=True`. Every one of these defeats a LibRaw behaviour
#: that would otherwise corrupt a measurement: `gamma`/`no_auto_bright` keep the output linear and
#: unscaled, `use_*_wb`/`user_wb` stop LibRaw guessing a white balance we want to measure ourselves,
#: and `output_color=raw` (see module docstring) is the one that is silently wrong if missed.
_POSTPROCESS_FLAGS = dict(
    gamma=(1, 1),
    no_auto_bright=True,
    output_bps=16,
    use_camera_wb=False,
    use_auto_wb=False,
    user_wb=[1, 1, 1, 1],
    output_color=rawpy.ColorSpace.raw,
    four_color_rgb=False,
)


@dataclass
class RawFrame:
    rgb: np.ndarray            # float64 (H,W,3) scene-linear, CAMERA-NATIVE primaries, 1.0 == saturation
    black: float               # black level actually used (counts)
    saturation: float          # counts treated as the ceiling
    dark_current: float        # margin median minus black, in counts
    clipped: np.ndarray        # bool (H,W), any channel at/above saturation
    clipped_fraction: float
    meta: dict                 # model, exposure_time, f_number, iso, focal_length


# --- pure-array logic — no file I/O, fully unit-testable without a sample .ARW --------------------

def bin2x2(raw_image: np.ndarray, pattern: np.ndarray) -> np.ndarray:
    """Average each 2x2 CFA block into one (R, G, B) triple: the default, non-demosaicing decode path.

    `pattern` is `rawpy`'s `raw_pattern` — a 2x2 array of colour-plane indices under `color_desc`
    (`RGBG`: 0=R, 1=G, 2=B, 3=G, the sensor's SECOND green site). Reading it rather than hardcoding
    positions is what makes this correct for any CFA phase, not just this one camera's `[[0,1],[3,2]]`.
    G is the mean of BOTH green sites — halving green's sample count relative to R and B would throw
    away exactly the channel human vision (and this project's luminance weighting) trusts most.
    """
    pattern = np.asarray(pattern)
    h, w = raw_image.shape
    h2, w2 = h // 2, w // 2
    img = raw_image[: h2 * 2, : w2 * 2].astype(np.float64)
    out = np.empty((h2, w2, 3), dtype=np.float64)
    green_sum = np.zeros((h2, w2), dtype=np.float64)
    green_n = 0
    for dy in (0, 1):
        for dx in (0, 1):
            site = img[dy::2, dx::2]
            color = int(pattern[dy, dx])
            if color == 0:
                out[..., 0] = site
            elif color == 2:
                out[..., 2] = site
            else:                      # 1 or 3 — RGBG's two green sites
                green_sum += site
                green_n += 1
    out[..., 1] = green_sum / green_n
    return out


def measure_black(margin: np.ndarray, pattern: np.ndarray) -> tuple[float, float]:
    """Split the FIXED electronic floor from THIS FRAME's dark current — never conflate them.

    `black` is not re-derived from the margin: deriving it from a per-frame low percentile would
    itself be corrupted by dark current at long exposures, because dark current raises the ENTIRE
    margin distribution, floor included, not just its tail (measured: the margin minimum itself rises
    from 504 at 1/3s to 540 at 8s, on the same sensor, same fixed electronic offset). So the floor is
    the known constant, and `dark_current` is simply what the margin's median sits above it — a scalar
    that CANNOT resolve hot pixels or spatial structure, which is exactly why `decode()` accepts an
    optional `dark_frame` for that.

    `pattern` is accepted (matching `bin2x2`'s call shape) but not split on: LibRaw reports this
    sensor's black level as identical across all four CFA sites, so pooling the margin into one
    scalar loses nothing a per-channel split would have recovered.
    """
    black = BLACK_LEVEL
    dark_current = float(np.median(margin)) - black
    return black, dark_current


def find_clipped(rgb_counts: np.ndarray, saturation: float) -> np.ndarray:
    """Any channel at/above `saturation` — AT, not strictly above. Saturated pixels pile up exactly at
    the empirical ceiling (see SATURATION in the module docstring); a strict `>` would silently pass
    the great majority of clipped pixels as fine."""
    clipped = np.asarray(rgb_counts) >= saturation
    return clipped.any(axis=-1) if clipped.ndim == 3 else clipped


# ⚠️ KNOWN LIMIT of running find_clipped on bin2x2 output: R and B come from a SINGLE CFA site each, so
# their clipping is detected exactly. G is the mean of two sites, so a bin where exactly ONE green site
# saturated averages back below the ceiling and reads as clean. The two green sites are diagonal
# neighbours and in practice saturate together, so this is narrow — but it is why `decode` checks
# clipping on the counts and never on anything downstream of a dark-frame subtraction.


# --- file I/O — everything above this line never touches a file -----------------------------------

def _channel_counts(raw: rawpy.RawPy, *, demosaic: bool) -> np.ndarray:
    """R, G, B in raw-ADU-equivalent counts, from either 2x2 binning or a full demosaic."""
    if not demosaic:
        return bin2x2(raw.raw_image_visible, raw.raw_pattern)
    out16 = raw.postprocess(**_POSTPROCESS_FLAGS)
    white = float(raw.white_level)
    # LibRaw's own output for these flags is (adu - black) / (white - black) * 65535. Undo that here so
    # a demosaiced frame lands in the SAME raw-ADU-equivalent units as bin2x2's output, and every bit of
    # black/saturation/clip arithmetic in `decode` below applies unchanged to either path.
    return out16.astype(np.float64) / 65535.0 * (white - BLACK_LEVEL) + BLACK_LEVEL


def _margin(raw: rawpy.RawPy) -> np.ndarray:
    """The light-shielded margin: 8 masked columns on the right, per the MEASURED constants above."""
    return raw.raw_image[:, raw.sizes.width: raw.sizes.raw_width]


def decode(path, *, dark_frame=None, demosaic: bool = False) -> RawFrame:
    """Load one `.ARW` into a `RawFrame`.

    `dark_frame`, if given, is a path to a lens-cap frame at matching exposure/ISO. Subtracting it
    per-pixel removes hot pixels and spatial dark-current structure that the scalar `dark_current`
    cannot — that scalar is diagnostic only and is never subtracted from `rgb` itself.
    """
    with rawpy.imread(str(path)) as raw:
        black, dark_current = measure_black(_margin(raw), raw.raw_pattern)
        counts = _channel_counts(raw, demosaic=demosaic)
        meta = {
            "model": CAMERA_MODEL,
            "exposure_time": raw.other.shutter_speed,
            "f_number": raw.other.aperture,
            "iso": raw.other.iso_speed,
            "focal_length": raw.other.focal_length,
        }

    # Clipping is a property of the SENSOR's own counts for this shot — checked before any dark-frame
    # subtraction, which would otherwise pull a saturated pixel's value back below `saturation` and
    # hide the fact that it clipped.
    clipped = find_clipped(counts, SATURATION)

    if dark_frame is not None:
        with rawpy.imread(str(dark_frame)) as dk:
            dark_counts = _channel_counts(dk, demosaic=demosaic)
        signal = counts - dark_counts     # black AND dark current cancel together, hot pixels included
    else:
        signal = counts - black

    rgb = signal / (SATURATION - black)
    return RawFrame(
        rgb=rgb, black=black, saturation=SATURATION, dark_current=dark_current,
        clipped=clipped, clipped_fraction=float(clipped.mean()), meta=meta,
    )


# --- CLI --------------------------------------------------------------------------------------------

def cmd_info(args) -> None:
    path = Path(args.path)
    with rawpy.imread(str(path)) as raw:
        print(f"file              {path}")
        print(f"raw_image         {raw.raw_image.shape} {raw.raw_image.dtype}")
        print(f"raw_image_visible {raw.raw_image_visible.shape}")
        print(f"margins           top={raw.sizes.top_margin} left={raw.sizes.left_margin}")
        print(f"color_desc        {raw.color_desc}")
        print(f"raw_pattern       {raw.raw_pattern.tolist()}")
        print(f"white_level       LibRaw reports {raw.white_level}   this module clips at {SATURATION:.0f}")
        # Fire only when LibRaw's number stops being the one we measured AGAINST. Warning on every
        # run because 16383 != 16372 would be an alarm that can only fire, which is the same defect
        # as a check that can only pass: it trains the reader to skip it, and then the one run that
        # matters looks like all the others.
        if raw.white_level != LIBRAW_WHITE_LEVEL:
            print(f"  ⚠️ WARN: LibRaw reports white_level {raw.white_level}, but SATURATION={SATURATION:.0f} "
                  f"was measured against a body reporting {LIBRAW_WHITE_LEVEL}. Something changed — a "
                  f"different camera, or a LibRaw upgrade. RE-VERIFY the empirical clip point before "
                  f"trusting any measurement from this frame.")

    frame = decode(path)
    print(f"black             {frame.black:.1f}")
    print(f"dark_current      {frame.dark_current:.1f}")
    print(f"clipped_fraction  {frame.clipped_fraction * 100:.4f}%")
    counts = frame.rgb * (frame.saturation - frame.black) + frame.black
    pcts = np.percentile(counts, [0, 1, 50, 99, 99.9, 100])
    names = ["0", "1", "50", "99", "99.9", "100"]
    pct_str = "  ".join(f"p{n}={v:.0f}" for n, v in zip(names, pcts))
    print(f"percentiles (raw-ADU-equivalent) {pct_str}")
    print(f"meta              {frame.meta}")


def cmd_selftest(args) -> None:
    print("SELF-TEST — pure-array checks against known-constructed data\n")
    ok = True

    # bin2x2: a hand-built 4x4 mosaic in the sensor's own [[0,1],[3,2]] phase, with known per-site
    # values, must recover exactly those values per channel — including that G averages BOTH sites.
    pattern = np.array([[0, 1], [3, 2]])
    mosaic = np.zeros((4, 4), dtype=np.float64)
    mosaic[0::2, 0::2] = 100.0   # R
    mosaic[0::2, 1::2] = 200.0   # G  (site 1)
    mosaic[1::2, 0::2] = 240.0   # G2 (site 3)
    mosaic[1::2, 1::2] = 300.0   # B
    binned = bin2x2(mosaic, pattern)
    want = np.array([100.0, 220.0, 300.0])   # green = mean(200, 240)
    case_ok = np.allclose(binned, want)
    ok &= case_ok
    print(f"  bin2x2 recovers known per-site values: {'OK' if case_ok else 'FAILED'} — got {binned[0, 0]}")

    # measure_black: floor is fixed; dark_current is the margin median above it, robust to a hot tail.
    rng = np.random.default_rng(0)
    margin = np.full(4000, BLACK_LEVEL + 24.0)
    margin[:5] = BLACK_LEVEL + 4000.0                          # a sparse hot-pixel tail
    margin = margin + rng.normal(0, 0.01, margin.shape)        # break exact ties, stay off the tail
    black, dark_current = measure_black(margin, pattern)
    case_ok = black == BLACK_LEVEL and abs(dark_current - 24.0) < 0.5
    ok &= case_ok
    print(f"  measure_black separates floor from dark current: {'OK' if case_ok else 'FAILED'} — "
          f"black={black} dark_current={dark_current:.2f}")

    # find_clipped: fires AT saturation, not strictly above it.
    counts = np.array([[SATURATION - 1, SATURATION, SATURATION + 1]])
    case_ok = list(find_clipped(counts, SATURATION)[0]) == [False, True, True]
    ok &= case_ok
    print(f"  find_clipped fires at >= saturation: {'OK' if case_ok else 'FAILED'}")

    print("\nself-test PASSED" if ok else "\nself-test FAILED")
    if not ok:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    info = sub.add_parser("info", help="report sensor constants, black/dark-current, clipping, percentiles")
    info.add_argument("path")
    sub.add_parser("selftest", help="validate the pure-array logic against known-constructed data")
    args = ap.parse_args()
    {"info": cmd_info, "selftest": cmd_selftest}[args.cmd](args)


if __name__ == "__main__":
    main()
