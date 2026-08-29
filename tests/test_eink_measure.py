"""The photograph -> measurement pipeline, validated against synthetic photographs.

Built and proven before any camera existed: a known perspective warp plus a known camera colour
distortion is applied to a known target, and the pipeline must recover the truth. That is a stronger
guarantee than pointing it at a panel and agreeing with the result, because here the answer is known.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

np = pytest.importorskip("numpy", reason="measurement tooling is maintainer-only, numpy not shipped")

import epaper as ep  # noqa: E402
from tools import eink_measure as em  # noqa: E402
from tools import eink_target as et  # noqa: E402

W, H = 800, 600


def _target():
    return et.compose(et.target_primaries(W, H), W, H)


def test_recovers_primaries_through_warp_and_camera_distortion():
    photo = em._synthesise_photo(_target(), warp=0.02, gain=(0.82, 0.96, 1.22),
                                 off=(20, 6, -12), noise=3.0, seed=7)
    r = em.read_panel(photo, W, H)
    got = em.measured_primaries(r["corrected"], W, H)
    for name, want in zip(et.INK_NAMES, ep.SPECTRA6_OUTPUT_PALETTE):
        worst = max(abs(a - b) for a, b in zip(got[name], want))
        assert worst <= 20, f"{name}: measured {got[name]} vs {list(want)}"


def test_finds_fiducials_not_the_dark_bezel():
    """The failure that cost real bench time: the first detector took the outermost dark rectangle,
    which on real hardware is the panel BEZEL, not the registration frame.

    _synthesise_photo pads with a dark bezel exactly like a real panel. Fiducials are drawn well
    inboard, so each detected centre must land near its expected inboard position — nowhere near the
    padded edge.
    """
    photo = em._synthesise_photo(_target(), warp=0.0, gain=(1, 1, 1), off=(0, 0, 0),
                                 noise=0.0, seed=1)
    pad = int(max(W, H) * 0.12)
    got = em.find_fiducials(photo, W, H)
    want = [(x + pad, y + pad) for x, y in et.fiducial_centres(W, H)]
    for (gx, gy), (wx, wy) in zip(got, want):
        assert abs(gx - wx) < 12 and abs(gy - wy) < 12, f"fiducial at ({gx:.0f},{gy:.0f}) want ({wx},{wy})"


def test_correction_is_solved_per_photograph():
    """Two shots under DIFFERENT light must both normalise back to the same truth — which is why
    the correction is solved from patches inside each frame rather than measured once."""
    t = _target()
    a = em.read_panel(em._synthesise_photo(t, 0.01, (0.75, 0.9, 1.3), (28, 10, -18), 2.0, 3), W, H)
    b = em.read_panel(em._synthesise_photo(t, 0.01, (1.15, 1.02, 0.85), (-12, -4, 14), 2.0, 4), W, H)
    assert a["gain"] != b["gain"], "different light must yield different corrections"
    pa = em.measured_primaries(a["corrected"], W, H)
    pb = em.measured_primaries(b["corrected"], W, H)
    for name in et.INK_NAMES:
        drift = max(abs(x - y) for x, y in zip(pa[name], pb[name]))
        assert drift <= 22, f"{name} disagrees across lighting: {pa[name]} vs {pb[name]}"


def test_panel_box_survives_perspective_without_splitting():
    """REGRESSION (2026-08-29). The panel box is seeded from the largest connected bright-neutral
    region, and what held that region together across the target's dark content bands was the 10 px
    outer white gutter — 0.625 of a 16 px coarse cell, barely over the 0.5 coverage threshold.

    Under perspective it fell below, six grid rows went empty, the region split, and the seed box
    came back as a fraction of the panel. Self-test case 4 (4% warp) had been failing since a20d785
    and the whole 2026-08-28 measurement session ran on it. Registration failure is silent: the
    numbers look like a mis-calibrated camera, not like a mis-registered frame.
    """
    for warp in (0.0, 0.02, 0.04):
        photo = em._synthesise_photo(_target(), warp=warp, gain=(0.70, 0.88, 1.35),
                                     off=(30, 12, -20), noise=6.0, seed=4)
        x0, y0, x1, y1 = em.panel_bbox(photo)
        assert (x1 - x0) > 0.85 * W and (y1 - y0) > 0.85 * H, (
            f"warp {warp}: seed box {x1 - x0}x{y1 - y0} is a fraction of the {W}x{H} panel")


def test_panel_is_found_under_a_strong_camera_colour_cast():
    """REGRESSION (2026-08-29). The panel is neutral in the WORLD, not in the photograph. The C920's
    white balance is locked to a fixed 4000 K that does not match the room, so the panel photographs
    with a cast; testing saturation on RAW pixels rejects the panel for being the colour the camera
    made it. 19 of 42 randomised synthetic captures failed this way, reporting "no neutral area
    found" with the panel plainly in shot.
    """
    for gain in ((0.70, 0.88, 1.35), (1.30, 1.00, 0.72), (0.95, 1.25, 0.80)):
        photo = em._synthesise_photo(_target(), warp=0.02, gain=gain, off=(20, 6, -12),
                                     noise=3.0, seed=11)
        r = em.read_panel(photo, W, H)          # must not raise
        assert r["patch_residual"] < 14, f"gain {gain}: residual {r['patch_residual']:.1f}"
