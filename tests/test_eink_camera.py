"""tools/eink_camera.py — proving the solver recovers a known camera, and that the hardcoded
ColorChecker reference tables are not hiding a transcription typo.

Everything here runs on synthetic data: a real photographed chart is a bench artefact this repo does
not carry, so what CAN be proven without one is (a) the reference numbers are internally sane, and
(b) `solve_camera_matrix` actually recovers a matrix it is handed a chart generated from.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

np = pytest.importorskip("numpy", reason="camera colorimetry tooling is maintainer-only, numpy not shipped")

from tools import eink_camera as ec  # noqa: E402
from tools.eink_color import ciede2000, lab_to_xyz, xyz_to_lab  # noqa: E402

TABLES = {"after_2014": ec.CC24_LAB_AFTER_2014, "before_2014": ec.CC24_LAB_BEFORE_2014}


# --- structural self-checks on the hardcoded reference data -----------------------------------------
# A transcription typo in 72 hand-entered numbers is the likeliest bug in this whole module, and would
# be invisible downstream (it would just quietly fit the wrong chart). These are the checks that would
# catch one.

@pytest.mark.parametrize("name,table", TABLES.items())
def test_reference_table_shape(name, table):
    assert table.shape == (24, 3)
    assert len(ec.CC24_NAMES) == 24


@pytest.mark.parametrize("name,table", TABLES.items())
def test_neutrals_are_near_neutral(name, table):
    """Patches 19-24 (rows 18-23) are the achromatic ramp: |a*| and |b*| must be small."""
    neutrals = table[ec.NEUTRAL_ROWS]
    assert np.all(np.abs(neutrals[:, 1]) < 3.0), f"[{name}] a* not near zero for a neutral patch"
    assert np.all(np.abs(neutrals[:, 2]) < 3.0), f"[{name}] b* not near zero for a neutral patch"


@pytest.mark.parametrize("name,table", TABLES.items())
def test_neutrals_are_strictly_monotonic_in_lightness(name, table):
    """The neutral ramp runs white(19) -> black(24): L* must strictly decrease down the ramp. A single
    transposed pair of rows would otherwise pass every other check here and still be silently wrong."""
    L = table[ec.NEUTRAL_ROWS, 0]
    assert np.all(np.diff(L) < 0.0), f"[{name}] neutral L* is not strictly decreasing: {L}"


@pytest.mark.parametrize("name,table", TABLES.items())
def test_every_lightness_in_range_and_every_patch_distinct(name, table):
    L = table[:, 0]
    assert np.all(L >= 0.0) and np.all(L <= 100.0), f"[{name}] L* outside [0,100]"
    rows = [tuple(row) for row in table.tolist()]
    assert len(set(rows)) == len(rows), f"[{name}] two patches share identical Lab — likely a copy/paste"


@pytest.mark.parametrize("name,table", TABLES.items())
def test_known_hue_quadrants(name, table):
    """Patches whose names name a hue must land in the hue quadrant that name implies. Chart numbering
    is 1-based; rows below are 0-based (row = patch number - 1)."""
    assert table[14, 1] > 0.0, f"[{name}] patch 15 'Red' must have a* > 0"       # red: +a*
    assert table[12, 2] < 0.0, f"[{name}] patch 13 'Blue' must have b* < 0"      # blue: -b*
    assert table[6, 1] > 0.0 and table[6, 2] > 0.0, (
        f"[{name}] patch 7 'Orange' must have a*>0, b*>0")                      # orange: +a*, +b*
    assert table[13, 1] < 0.0, f"[{name}] patch 14 'Green' must have a* < 0"     # green: -a*
    assert table[17, 1] < 0.0 and table[17, 2] < 0.0, (
        f"[{name}] patch 18 'Cyan' must have a*<0, b*<0")                       # cyan: -a*, -b*


def test_before_and_after_2014_tables_are_close_but_not_identical():
    """The 2014 reformulation changed the pigments, not the chart's whole design: same names, same
    chart layout, small per-patch differences — not a different chart entirely, and not a copy-paste
    of one table into the other (which would defeat the whole point of carrying both)."""
    diff = np.abs(ec.CC24_LAB_AFTER_2014 - ec.CC24_LAB_BEFORE_2014)
    assert not np.allclose(ec.CC24_LAB_AFTER_2014, ec.CC24_LAB_BEFORE_2014), (
        "before/after tables must not be identical — that would mean one is a copy of the other")
    assert diff.max() < 15.0, "before/after tables differ far more than a pigment reformulation should"


# --- the solver ---------------------------------------------------------------------------------------

TRUE_M = ec._TRUE_CAMERA_TO_XYZ


def _synth(reference_lab=ec.CC24_LAB_AFTER_2014, **kw):
    return ec._synthesise_camera_rgb(reference_lab, TRUE_M, **kw)


def test_solve_camera_matrix_recovers_known_matrix_exactly():
    """A camera that IS a linear map (no noise, no non-linearity) must be recovered to numerical
    precision, and the chart residual must be near zero — this is the case where a linear model is
    exactly correct, so nothing should be left unexplained."""
    rgb = _synth()
    M, report = ec.solve_camera_matrix(rgb)
    assert np.allclose(M, TRUE_M, atol=1e-6)
    assert report["mean"] < 1e-3
    assert report["median"] < 1e-3
    assert report["worst"] < 1e-2
    assert report["de00"].shape == (24,)
    assert report["worst_patch"] in ec.CC24_NAMES


def test_solve_camera_matrix_report_worst_patch_matches_argmax():
    rgb = _synth(noise=0.01, seed=3)
    M, report = ec.solve_camera_matrix(rgb)
    worst_idx = int(np.argmax(report["de00"]))
    assert report["worst_patch"] == ec.CC24_NAMES[worst_idx]
    assert report["worst"] == pytest.approx(float(report["de00"][worst_idx]))


def test_noise_robustness_residual_rises_but_stays_small_and_matrix_stays_close():
    """Small sensor noise should nudge the fit, not break it: residual goes up from the near-zero
    clean case but stays within a couple of dE00, and the recovered matrix stays close to truth."""
    rgb_clean = _synth()
    _, report_clean = ec.solve_camera_matrix(rgb_clean)

    rgb_noisy = _synth(noise=0.002, seed=1)
    M_noisy, report_noisy = ec.solve_camera_matrix(rgb_noisy)

    assert report_noisy["mean"] > report_clean["mean"]
    assert report_noisy["mean"] < 2.0
    assert np.abs(M_noisy - TRUE_M).max() < 0.1


def test_nonlinear_camera_produces_visibly_worse_residual():
    """A per-channel-gamma 'camera' cannot be represented exactly by any 3x3: the residual must come
    out MUCH worse than the clean linear case. This is the check that proves the residual is
    diagnostic — a metric that reports 'fine' regardless of input is a check that can only pass."""
    rgb_linear = _synth()
    _, report_linear = ec.solve_camera_matrix(rgb_linear)

    rgb_gamma = _synth(gamma=1.8)
    _, report_gamma = ec.solve_camera_matrix(rgb_gamma)

    assert report_gamma["mean"] > 1.0
    assert report_gamma["mean"] > 20 * max(report_linear["mean"], 1e-9)


def test_solve_camera_matrix_rejects_mismatched_shapes():
    rgb = _synth()[:20]   # wrong patch count vs. the 24-row default reference
    with pytest.raises(ValueError):
        ec.solve_camera_matrix(rgb)


def test_apply_camera_matrix_matches_manual_matrix_multiply():
    rgb = _synth()
    M, _ = ec.solve_camera_matrix(rgb)
    xyz_a = ec.apply_camera_matrix(rgb, M)
    xyz_b = rgb @ M.T
    assert np.allclose(xyz_a, xyz_b)


def test_camera_rgb_to_lab_matches_manual_pipeline():
    rgb = _synth()
    M, _ = ec.solve_camera_matrix(rgb)
    lab_a = ec.camera_rgb_to_lab(rgb, M)
    lab_b = xyz_to_lab(ec.apply_camera_matrix(rgb, M), ec.D50)
    assert np.allclose(lab_a, lab_b)


def test_solve_camera_matrix_de00_matches_manual_ciede2000():
    """The report's per-patch ΔE00 must be exactly what `eink_color.ciede2000` would compute between
    the solved-and-reapplied Lab and the reference Lab — no re-implementation of colour maths here."""
    rgb = _synth(noise=0.01, seed=5)
    M, report = ec.solve_camera_matrix(rgb)
    predicted_lab = xyz_to_lab(ec.apply_camera_matrix(rgb, M), ec.D50)
    expected = np.asarray(ciede2000(predicted_lab, ec.CC24_LAB_AFTER_2014))
    assert np.allclose(report["de00"], expected)


def test_solve_camera_matrix_accepts_before_2014_table():
    rgb = _synth(reference_lab=ec.CC24_LAB_BEFORE_2014)
    M, report = ec.solve_camera_matrix(rgb, reference_lab=ec.CC24_LAB_BEFORE_2014)
    assert np.allclose(M, TRUE_M, atol=1e-6)
    assert report["mean"] < 1e-3


def test_lab_to_xyz_D50_round_trips_through_eink_color():
    """Sanity check that this module's D50 white and eink_color's Lab<->XYZ conversion agree with each
    other on a round trip — not re-deriving Lab maths, just confirming the wiring."""
    xyz = lab_to_xyz(ec.CC24_LAB_AFTER_2014, ec.D50)
    lab_back = xyz_to_lab(xyz, ec.D50)
    assert np.allclose(lab_back, ec.CC24_LAB_AFTER_2014, atol=1e-8)


# --- CLI --------------------------------------------------------------------------------------------

def test_cmd_selftest_passes(capsys):
    ec.cmd_selftest(None)   # cmd_selftest takes an argparse Namespace but never reads it
    out = capsys.readouterr().out
    assert "self-test PASSED" in out
