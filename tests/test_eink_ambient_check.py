"""tools/eink/eink_ambient_check.py — the daytime-shoot gate (ADR-114).

Pure-array logic checked against constructed data, plus one check against the real 2026-09-04
ambient pair (DSC00268 lit / DSC00271 lamp-off) where those samples happen to be on disk: that pair
is the 40% figure ADR-112 rests on, so the tool must reproduce it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

np = pytest.importorskip("numpy", reason="rig tooling is maintainer-only, numpy not shipped")

from tools.eink import eink_ambient_check as ac  # noqa: E402

LINEUP = Path(__file__).resolve().parent.parent / "bench-eink" / "camera" / "nex6-lineup-2026-09-04"
LIT_0904 = LINEUP / "DSC00268.ARW"     # 30 s, lamp on (the tool scales the 20 s dark frame to it)
DARK_0904 = LINEUP / "DSC00271.ARW"    # 20 s, lamp off — the 40% frame
CAP_0904 = LINEUP / "DSC00270.ARW"     # 20 s, lens cap ON by accident — must NOT read as GO


def test_block_map_medians_each_cell():
    plane = np.zeros((60, 80))
    plane[:30, :40] = 10
    plane[:30, 40:] = 20
    plane[30:, :40] = 30
    plane[30:, 40:] = 40
    assert np.array_equal(ac.block_map(plane, (0, 0, 80, 60), (2, 2)), [[10, 20], [30, 40]])


def test_block_map_respects_roi_offset():
    plane = np.zeros((60, 80))
    plane[10:50, 20:60] = 7
    bm = ac.block_map(plane, (20, 10, 60, 50), (2, 2))
    assert np.all(bm == 7)


def test_block_map_refuses_roi_smaller_than_grid():
    with pytest.raises(ValueError):
        ac.block_map(np.zeros((4, 4)), (0, 0, 4, 4), (6, 8))


def test_trim_roi_drops_non_panel_edges_and_keeps_a_clean_roi():
    plane = np.full((120, 160), 1000.0)
    plane[100:, :] = 50.0      # tray under the panel
    plane[:, :20] = 30.0       # flock beside it
    assert ac.trim_roi_to_lit(plane, (0, 0, 160, 120), (6, 8)) == (20, 0, 160, 100)
    assert ac.trim_roi_to_lit(plane, (20, 0, 160, 100), (6, 8)) == (20, 0, 160, 100)


def test_ambient_fraction_scales_to_lit_exposure():
    # 100 ADU at 10 s is 200 ADU at the lit frame's 20 s; over 1000 lit ADU that is 20%.
    assert ac.ambient_fraction(100, 1000, 10, 20) == pytest.approx(0.2)
    assert ac.ambient_fraction(400, 1000, 20, 20) == pytest.approx(0.4)


def test_ambient_fraction_refuses_dead_lit_frame():
    with pytest.raises(ValueError):
        ac.ambient_fraction(10, 0, 20, 20)


def test_verdict_boundaries_are_inclusive():
    assert ac.verdict(0.030) == "GO"
    assert ac.verdict(0.0301) == "BRACKET"
    assert ac.verdict(0.100) == "BRACKET"
    assert ac.verdict(0.1001) == "NO-GO"
    assert ac.verdict(0.40) == "NO-GO", "the 2026-09-04 room must be a NO-GO"


def test_cap_on_detector_matches_dsc00270_shape():
    # DSC00270 read +1.9..+2.4 ADU, flat, no structure (ADR-112 addendum 3).
    assert ac.looks_like_cap_on(np.random.uniform(1.9, 2.4, size=(6, 8)))
    assert not ac.looks_like_cap_on(np.full((6, 8), 410.0))
    m = np.full((6, 8), 1.0)
    m[2, 3] = 30.0    # one mirrored window is enough to prove the frame can see
    assert not ac.looks_like_cap_on(m)


def test_shape_drift_ignores_uniform_gain_and_sees_one_block():
    a = np.full((6, 8), 1000.0)
    _, d = ac.shape_drift(a, a * 1.4)
    assert d == pytest.approx(0.0)
    b = a.copy()
    b[5, 7] *= 0.93
    _, d = ac.shape_drift(a, b)
    assert d == pytest.approx(0.07)


def test_shape_drift_refuses_dead_reference():
    with pytest.raises(ValueError):
        ac.shape_drift(np.zeros((2, 2)), np.ones((2, 2)))


@pytest.mark.skipif(not (LIT_0904.exists() and DARK_0904.exists()), reason="09-04 lineup not on disk")
def test_reproduces_the_2026_09_04_forty_percent():
    pytest.importorskip("rawpy")
    lit, dark = ac._load(LIT_0904), ac._load(DARK_0904)
    roi = ac._locate_panel(lit)
    lp, dp = ac._adu_plane(lit), ac._adu_plane(dark)
    lit_med = float(np.median(lp[roi[1]:roi[3], roi[0]:roi[2]]))
    dk_med = float(np.median(dp[roi[1]:roi[3], roi[0]:roi[2]]))
    frac = ac.ambient_fraction(dk_med, lit_med, dark.meta["exposure_time"], lit.meta["exposure_time"])
    assert 0.25 <= frac <= 0.55, f"ADR-112 says ~40%; tool says {frac * 100:.1f}%"
    assert ac.verdict(frac) == "NO-GO"
    assert not ac.looks_like_cap_on(ac.block_map(dp, roi))


@pytest.mark.skipif(not (LIT_0904.exists() and CAP_0904.exists()), reason="09-04 lineup not on disk")
def test_the_lens_cap_frame_is_flagged_not_passed():
    pytest.importorskip("rawpy")
    lit, cap = ac._load(LIT_0904), ac._load(CAP_0904)
    roi = ac._locate_panel(lit)
    assert ac.looks_like_cap_on(ac.block_map(ac._adu_plane(cap), roi))
