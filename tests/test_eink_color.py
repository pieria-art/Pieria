"""Correctness anchor for the colour foundation — provable with ZERO project data.

Everything the physics-first render model concludes rests on this module being right. So it is not
checked against anything of ours: it is checked against published reference values, and against
identities that hold by definition. This is the one test file in the repo that could be run in an
empty repository and still mean something.
"""
import numpy as np
import pytest

from tools.eink_color import (
    D65,
    ciede2000,
    lab_to_lch,
    lab_to_xyz,
    lch_to_lab,
    linear_rgb_to_xyz,
    linear_to_srgb,
    relative_luminance,
    srgb8_to_xyz,
    srgb_to_linear,
    xyz_to_lab,
    xyz_to_linear_rgb,
)

# Sharma, Wu & Dalal (2005), "The CIEDE2000 color-difference formula: implementation notes,
# supplementary test data, and mathematical observations", Table 1. 34 pairs, chosen so that the
# branchy parts of the formula are actually exercised:
#   1-4    the a*=0 axis and the arctan quadrant boundary
#   9-16   ZERO CHROMA, where h' is undefined and the mean-hue branch takes the SUM, not the average
#   17-24  large differences, and the four unit-difference cases that pin S_L/S_C/S_H
#   25-34  real surface colours; 29-34 sit in the blue region where R_T is nonzero
# (Lab1, Lab2, expected dE00)
SHARMA = [
    ((50.0000,   2.6772, -79.7751), (50.0000,   0.0000, -82.7485), 2.0425),
    ((50.0000,   3.1571, -77.2803), (50.0000,   0.0000, -82.7485), 2.8615),
    ((50.0000,   2.8361, -74.0200), (50.0000,   0.0000, -82.7485), 3.4412),
    ((50.0000,  -1.3802, -84.2814), (50.0000,   0.0000, -82.7485), 1.0000),
    ((50.0000,  -1.1848, -84.8006), (50.0000,   0.0000, -82.7485), 1.0000),
    ((50.0000,  -0.9009, -85.5211), (50.0000,   0.0000, -82.7485), 1.0000),
    ((50.0000,   0.0000,   0.0000), (50.0000,  -1.0000,   2.0000), 2.3669),
    ((50.0000,  -1.0000,   2.0000), (50.0000,   0.0000,   0.0000), 2.3669),
    ((50.0000,   2.4900,  -0.0010), (50.0000,  -2.4900,   0.0009), 7.1792),
    ((50.0000,   2.4900,  -0.0010), (50.0000,  -2.4900,   0.0010), 7.1792),
    ((50.0000,   2.4900,  -0.0010), (50.0000,  -2.4900,   0.0011), 7.2195),
    ((50.0000,   2.4900,  -0.0010), (50.0000,  -2.4900,   0.0012), 7.2195),
    ((50.0000,  -0.0010,   2.4900), (50.0000,   0.0009,  -2.4900), 4.8045),
    ((50.0000,  -0.0010,   2.4900), (50.0000,   0.0010,  -2.4900), 4.8045),
    ((50.0000,  -0.0010,   2.4900), (50.0000,   0.0011,  -2.4900), 4.7461),
    ((50.0000,   2.5000,   0.0000), (50.0000,   0.0000,  -2.5000), 4.3065),
    ((50.0000,   2.5000,   0.0000), (73.0000,  25.0000, -18.0000), 27.1492),
    ((50.0000,   2.5000,   0.0000), (61.0000,  -5.0000,  29.0000), 22.8977),
    ((50.0000,   2.5000,   0.0000), (56.0000, -27.0000,  -3.0000), 31.9030),
    ((50.0000,   2.5000,   0.0000), (58.0000,  24.0000,  15.0000), 19.4535),
    ((50.0000,   2.5000,   0.0000), (50.0000,   3.1736,   0.5854), 1.0000),
    ((50.0000,   2.5000,   0.0000), (50.0000,   3.2972,   0.0000), 1.0000),
    ((50.0000,   2.5000,   0.0000), (50.0000,   1.8634,   0.5757), 1.0000),
    ((50.0000,   2.5000,   0.0000), (50.0000,   3.2592,   0.3350), 1.0000),
    ((60.2574, -34.0099,  36.2677), (60.4626, -34.1751,  39.4387), 1.2644),
    ((63.0109, -31.0961,  -5.8663), (62.8187, -29.7946,  -4.0864), 1.2630),
    ((61.2901,   3.7196,  -5.3901), (61.4292,   2.2480,  -4.9620), 1.8731),
    ((35.0831, -44.1164,   3.7933), (35.0232, -40.0716,   1.5901), 1.8645),
    ((22.7233,  20.0904, -46.6940), (23.0331,  14.9730, -42.5619), 2.0373),
    ((36.4612,  47.8580,  18.3852), (36.2715,  50.5065,  21.2231), 1.4146),
    ((90.8027,  -2.0831,   1.4410), (91.1528,  -1.6435,   0.0447), 1.4441),
    ((90.9257,  -0.5406,  -0.9208), (88.6381,  -0.8985,  -0.7239), 1.5381),
    (( 6.7747,  -0.2908,  -2.4247), ( 5.8714,  -0.0985,  -2.2286), 0.6377),
    (( 2.0776,   0.0795,  -1.1350), ( 0.9033,  -0.0636,  -0.5514), 0.9082),
]


@pytest.mark.parametrize("lab1,lab2,expected", SHARMA,
                         ids=[f"sharma{i + 1:02d}" for i in range(len(SHARMA))])
def test_ciede2000_matches_sharma_reference(lab1, lab2, expected):
    assert float(ciede2000(lab1, lab2)) == pytest.approx(expected, abs=1e-4)


def test_ciede2000_is_symmetric_and_zero_on_identity():
    rng = np.random.default_rng(20260829)
    lab = np.stack([rng.uniform(0, 100, 500), rng.uniform(-100, 100, 500),
                    rng.uniform(-100, 100, 500)], axis=-1)
    other = np.roll(lab, 1, axis=0)
    assert np.allclose(ciede2000(lab, lab), 0.0, atol=1e-12)
    assert np.allclose(ciede2000(lab, other), ciede2000(other, lab), atol=1e-12)


def test_ciede2000_broadcasts():
    # The objective evaluates this per pixel over 1.92 Mpx; a scalar-only implementation is unusable.
    a = np.zeros((4, 5, 3)) + np.array([50.0, 2.5, 0.0])
    b = np.zeros((4, 5, 3)) + np.array([50.0, 0.0, -2.5])
    out = ciede2000(a, b)
    assert out.shape == (4, 5)
    assert np.allclose(out, 4.3065, atol=1e-4)


def test_srgb_transfer_round_trips_every_8bit_code_exactly():
    codes = np.arange(256) / 255.0
    assert np.allclose(linear_to_srgb(srgb_to_linear(codes)), codes, atol=1e-12)


def test_srgb_transfer_is_piecewise_not_a_22_power():
    """The x**2.2 shortcut is wrong, and wrong in the RATIOS, which is what shadows are made of.

    ⚠️ Measured, after an earlier version of this test asserted the opposite and was refuted: the
    ABSOLUTE error is roughly flat across the range (~0.004-0.006, in fact slightly larger at the top).
    It is the RELATIVE error that concentrates in the shadows — x**2.2 understates radiance by 19.4x at
    x=0.01 and 2.9x at x=0.05, converging to 1.0 by midtone. That is the number that matters here: this
    panel is starved at the dark end, and shadow modelling lives in the ratios between dark tones, not
    in their absolute separation.
    """
    x = np.linspace(0.0, 1.0, 256)
    lin, approx = srgb_to_linear(x), x ** 2.2
    assert np.abs(lin - approx).max() > 0.005, "curves must differ, else the piecewise branch is dead"

    ratio = lin[1:] / approx[1:]                       # skip x=0, where both are 0
    assert ratio[0] > 15.0, "x**2.2 must badly understate the darkest codes"
    assert np.allclose(ratio[x[1:] > 0.4], 1.0, atol=0.05), "and must agree by the midtones"


def test_xyz_round_trip_and_white_point():
    rng = np.random.default_rng(7)
    rgb = rng.uniform(0, 1, (200, 3))
    assert np.allclose(xyz_to_linear_rgb(linear_rgb_to_xyz(rgb)), rgb, atol=1e-12)
    # D65 is the sRGB white by construction: linear (1,1,1) must land exactly on it.
    assert np.allclose(linear_rgb_to_xyz([1.0, 1.0, 1.0]), D65, atol=1e-12)


def test_lab_anchors_and_round_trip_including_the_linear_branch():
    assert np.allclose(xyz_to_lab(D65, D65), [100.0, 0.0, 0.0], atol=1e-9)
    assert np.allclose(xyz_to_lab([0.0, 0.0, 0.0], D65), [0.0, 0.0, 0.0], atol=1e-12)
    # Published anchor: sRGB pure red under D65.
    assert np.allclose(xyz_to_lab(srgb8_to_xyz([255, 0, 0]), D65),
                       [53.2408, 80.0925, 67.2032], atol=1e-4)
    # Round-trip must hold BELOW the (6/29)^3 knee too — that is the branch a cube-root-only
    # implementation gets wrong, and it is where this panel's shadows live.
    lab = np.array([[0.5, 0.2, -0.3], [2.0, -1.0, 1.0], [50.0, 20.0, -30.0], [99.0, -5.0, 5.0]])
    assert np.allclose(xyz_to_lab(lab_to_xyz(lab, D65), D65), lab, atol=1e-10)


def test_lch_round_trip():
    lab = np.array([[50.0, 20.0, -30.0], [10.0, -40.0, 5.0], [90.0, 0.0, 0.0]])
    assert np.allclose(lch_to_lab(lab_to_lch(lab)), lab, atol=1e-12)


def test_relative_luminance_is_not_the_flat_channel_mean():
    """The defect this module exists to remove, pinned as a test.

    For the panel's own palette, the flat RGB mean ranks white above yellow; real luminance ranks
    yellow above white by 38%. Every "the ceiling is the white ink" statement in this project
    inherits the flat mean's inversion.
    """
    white, yellow = (161, 164, 165), (208, 190, 71)
    assert np.mean(white) > np.mean(yellow)                      # the old, wrong ordering
    assert relative_luminance(yellow) > relative_luminance(white)  # the real one
    assert float(relative_luminance(yellow) / relative_luminance(white)) == pytest.approx(1.38, abs=0.01)
