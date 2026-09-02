"""tools/eink_raw.py — pure-array logic checked against known-constructed data, plus a handful of
checks against real Sony NEX-6 `.ARW` samples where those samples happen to be on disk.

The pure-array tests must pass with NO sample files present: bin2x2/measure_black/find_clipped never
touch a file, which is the whole point of keeping them separate from `decode`.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

np = pytest.importorskip("numpy", reason="raw ingest tooling is maintainer-only, numpy not shipped")
pytest.importorskip("rawpy", reason="raw ingest tooling is maintainer-only, rawpy not shipped")

from tools import eink_raw as er  # noqa: E402

PATTERN = np.array([[0, 1], [3, 2]])   # this sensor's RGBG phase: (0,0)=R (0,1)=G (1,0)=G2 (1,1)=B

SAMPLES = Path(__file__).resolve().parent.parent / "bench-eink" / "camera" / "nex6-samples-2026-09-01"
DARK_3S2 = SAMPLES / "DSC00235.ARW"    # 3.2s
SHORT = SAMPLES / "DSC00236.ARW"       # 1/3s, no clipping
LONG = SAMPLES / "DSC00238.ARW"        # 8s, clips


def test_bin2x2_recovers_known_per_channel_values():
    mosaic = np.zeros((4, 4), dtype=np.float64)
    mosaic[0::2, 0::2] = 100.0   # R site
    mosaic[0::2, 1::2] = 200.0   # G site
    mosaic[1::2, 0::2] = 240.0   # G2 site
    mosaic[1::2, 1::2] = 300.0   # B site
    out = er.bin2x2(mosaic, PATTERN)
    assert out.shape == (2, 2, 3)
    assert np.allclose(out[..., 0], 100.0)
    assert np.allclose(out[..., 1], 220.0), "G must be the mean of BOTH green sites, (200+240)/2"
    assert np.allclose(out[..., 2], 300.0)


def test_bin2x2_halves_each_dimension_and_drops_odd_remainder():
    mosaic = np.zeros((5, 7), dtype=np.float64)   # odd dims: the trailing row/col must be dropped
    mosaic[0::2, 0::2] = 100.0
    mosaic[0::2, 1::2] = 200.0
    mosaic[1::2, 0::2] = 240.0
    mosaic[1::2, 1::2] = 300.0
    out = er.bin2x2(mosaic, PATTERN)
    assert out.shape == (2, 3, 3)


def test_measure_black_recovers_known_black_and_dark_current():
    rng = np.random.default_rng(0)
    dark_current_truth = 24.0
    margin = np.full(4000, er.BLACK_LEVEL + dark_current_truth)
    margin[:5] = er.BLACK_LEVEL + 4000.0                    # sparse hot-pixel tail
    margin = margin + rng.normal(0, 0.01, margin.shape)     # break exact ties without moving the median
    black, dark_current = er.measure_black(margin, PATTERN)
    assert black == er.BLACK_LEVEL
    assert abs(dark_current - dark_current_truth) < 0.1, (
        "a sparse hot-pixel tail must not drag the median-based dark_current estimate")


def test_measure_black_dark_current_scales_with_synthetic_exposure():
    short = er.BLACK_LEVEL + 8.0 + np.zeros(2000)
    long = er.BLACK_LEVEL + 424.0 + np.zeros(2000)
    _, dc_short = er.measure_black(short, PATTERN)
    _, dc_long = er.measure_black(long, PATTERN)
    assert dc_long > dc_short


def test_find_clipped_fires_at_saturation_not_above():
    counts = np.array([[er.SATURATION - 1, er.SATURATION, er.SATURATION + 1]])
    got = er.find_clipped(counts, er.SATURATION)[0]
    assert list(got) == [False, True, True]


def test_find_clipped_any_channel_over_last_axis():
    counts = np.zeros((1, 2, 3))
    counts[0, 0, 1] = er.SATURATION    # only the G channel of the first pixel clips
    got = er.find_clipped(counts, er.SATURATION)
    assert got.shape == (1, 2)
    assert got[0, 0] and not got[0, 1]


def test_dark_frame_subtraction_removes_hot_pixel_a_scalar_black_does_not():
    """The reason `decode` accepts `dark_frame`: a scalar black level is uniform, so it cannot touch a
    SPATIAL defect like a hot pixel — only subtracting an actual per-pixel dark frame can.
    """
    light = np.full((4, 4), er.BLACK_LEVEL + 200.0)   # flat scene signal everywhere
    dark = np.full((4, 4), er.BLACK_LEVEL)            # lens-cap frame: no signal, just floor...
    light[0, 0] += 3000.0                             # ...except a hot pixel present in BOTH frames
    dark[0, 0] += 3000.0                              # (hot pixels are a sensor defect, not scene light)

    light_counts = er.bin2x2(light, PATTERN)
    dark_counts = er.bin2x2(dark, PATTERN)

    scalar_corrected = light_counts - er.BLACK_LEVEL
    dark_corrected = light_counts - dark_counts

    # The hot pixel sits in the top-left 2x2 CFA block, i.e. binned pixel (0, 0).
    assert scalar_corrected[0, 0, 0] > 1000.0, "a scalar black must leave the hot pixel elevated"
    assert abs(dark_corrected[0, 0, 0] - 200.0) < 1.0, "dark-frame subtraction must remove it"
    # Everywhere else, both corrections agree (no hot pixel there).
    assert np.allclose(scalar_corrected[1:, 1:], dark_corrected[1:, 1:])


# --- real-file checks -------------------------------------------------------------------------------

pytestmark_samples = pytest.mark.skipif(
    not (SHORT.exists() and LONG.exists() and DARK_3S2.exists()),
    reason=f"NEX-6 sample .ARW files not present under {SAMPLES}",
)


@pytestmark_samples
def test_decode_succeeds_and_shape_is_binned():
    frame = er.decode(SHORT)
    assert frame.rgb.shape == (1638, 2460, 3)


@pytestmark_samples
def test_clipped_fraction_reflects_real_exposure():
    long_frame = er.decode(LONG)
    short_frame = er.decode(SHORT)
    assert long_frame.clipped_fraction > 0, "the 8s frame is known to clip"
    assert short_frame.clipped_fraction == 0, "the 1/3s frame is known not to clip"


@pytestmark_samples
def test_dark_current_is_monotone_in_exposure_time():
    """The margin must be reading DARK CURRENT, not noise — proven by monotonicity across a real
    exposure sweep at fixed ISO/aperture, not merely by an arbitrary number matching."""
    long_frame = er.decode(LONG)      # 8s
    short_frame = er.decode(SHORT)    # 1/3s
    assert long_frame.dark_current > short_frame.dark_current
