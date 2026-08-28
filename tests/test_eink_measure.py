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


def test_finds_the_frame_not_the_dark_bezel():
    """The bug the self-test caught: a naive dark-extremes search locks onto the dark surround.

    _synthesise_photo pads with a dark bezel exactly like a real panel, so this fails loudly if the
    panel-first detection is ever removed. Corners must land near the render's own frame, not out at
    the padded edge.
    """
    photo = em._synthesise_photo(_target(), warp=0.0, gain=(1, 1, 1), off=(0, 0, 0),
                                 noise=0.0, seed=1)
    pad = int(max(W, H) * 0.12)
    corners = em.find_frame_corners(photo)
    for x, y in corners:
        assert pad - 4 <= x <= photo.width - pad + 4, f"corner x={x} escaped to the bezel"
        assert pad - 4 <= y <= photo.height - pad + 4, f"corner y={y} escaped to the bezel"


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
