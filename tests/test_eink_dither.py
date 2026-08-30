"""S2 verification — the linear-light dither.

The load-bearing tests are the two that could refute the whole premise: that the new code CONTAINS the
old behaviour (so any difference is the defect and not a bug), and that radiance is conserved in linear
light while it demonstrably is not in gamma space.
"""
import numpy as np
import pytest
from PIL import Image

import epaper as ep
from tools import eink_color as ec
from tools import eink_dither as ed
from tools import eink_panel_model as pm


@pytest.fixture(scope="module")
def lut():
    return ed.nearest_ink_lut()


def _realised_L(idx):
    lin = ec.xyz_to_linear_rgb(pm.ink_xyz())[idx].reshape(-1, 3).mean(axis=0)
    return float(ec.xyz_to_lab(ec.linear_rgb_to_xyz(lin), pm.media_white())[0])


def _pillow(d, size=160):
    im = Image.new("RGB", (size, size), (d, d, d))
    pal = ep._cached_palette_image("_spectra6_dither", ep.SPECTRA6_DITHER_PALETTE)
    return np.asarray(im.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG))


def test_legacy_mode_reproduces_pillow_tone():
    """R1: the new code must CONTAIN the incumbent, or a later difference is a bug, not a finding.

    Agreement is asserted on the realised TONE, not per pixel: Pillow's C path uses integer error terms
    and its own tie-breaking, so the dither PATTERN differs on most pixels while the mean it integrates
    to does not. That is also A1's standing rule for this project — compare aggregates, never a cell.
    Measured worst disagreement across the range: 0.67 L*, against a per-pixel disagreement of up to 70%.
    """
    for d in range(0, 256, 24):
        a = np.full((160, 160, 3), d, dtype=np.uint8)
        assert _realised_L(ed.dither(a, mode="legacy")) == pytest.approx(_realised_L(_pillow(d)), abs=1.0)


def test_linear_mode_conserves_radiance_and_legacy_does_not(lut):
    """The cleanest one-line statement of the defect.

    Error diffusion conserves whatever it accumulates in. In linear mode that is radiance, so the
    identity sum(target) - sum(realised) == error-off-the-edge holds to machine precision. Gamma-space
    diffusion conserves encoded values instead, so the same radiance identity fails — by an amount that
    IS the defect.
    """
    rng = np.random.default_rng(20260829)
    a = rng.integers(0, 256, (96, 96, 3), dtype=np.uint8)
    inks_lin = ec.xyz_to_linear_rgb(pm.ink_xyz())
    target_sum = ec.srgb_to_linear(a.astype(np.float64) / 255.0).reshape(-1, 3).sum(axis=0)

    idx = ed.dither(a, mode="linear", lut=lut)
    imbalance = target_sum - inks_lin[idx].reshape(-1, 3).sum(axis=0) - ed.dither.last_lost
    assert np.abs(imbalance).max() < 1e-6, f"linear mode must conserve radiance, off by {imbalance}"

    idx_g = ed.dither(a, mode="legacy")
    resid = np.abs(target_sum - inks_lin[idx_g].reshape(-1, 3).sum(axis=0)).max()
    assert resid > 1.0, "gamma-space diffusion must NOT conserve radiance; if it did there is no defect"


def test_the_difference_between_modes_matches_S1_independently(lut):
    """R2: two independent code paths must agree on the size of the defect.

    S1 measured it as (Pillow's realised radiance) - (the source's own L*), peaking at +13.1 L* at
    d=24. This measures it as (legacy L*) - (linear L*) using neither Pillow nor the source. Same
    quantity, no shared code beyond the colour module. A disagreement here refutes one of them.
    """
    best_d, best_err = None, -1.0
    for d in range(8, 168, 8):
        a = np.full((160, 160, 3), d, dtype=np.uint8)
        err = _realised_L(ed.dither(a, mode="legacy")) - _realised_L(ed.dither(a, mode="linear", lut=lut))
        if err > best_err:
            best_d, best_err = d, err
    s1 = pm.dither_error_report()["prediction_holds"]
    assert best_err == pytest.approx(s1["peak_error_L"], abs=2.0), \
        f"S2 says peak {best_err:.2f} L*, S1 says {s1['peak_error_L']:.2f} — one of them is wrong"
    assert abs(best_d - s1["peak_at_d"]) <= 8, f"peaks at different levels: {best_d} vs {s1['peak_at_d']}"


def test_linear_mode_realises_the_source_radiance_where_it_is_achievable(lut):
    """A correct dither reproduces the target tone. Below the media ceiling it should land on the
    source's own lightness; above it, it can only reach the paper white — and must."""
    for d in (24, 48, 96, 144):
        a = np.full((160, 160, 3), d, dtype=np.uint8)
        want = float(ec.xyz_to_lab(ec.srgb8_to_xyz([d, d, d]), pm.media_white())[0])
        assert _realised_L(ed.dither(a, mode="linear", lut=lut)) == pytest.approx(want, abs=1.5)
    for d in (200, 255):
        a = np.full((160, 160, 3), d, dtype=np.uint8)
        assert _realised_L(ed.dither(a, mode="linear", lut=lut)) == pytest.approx(100.0, abs=0.5)


@pytest.mark.parametrize("i,name", list(enumerate(pm.INK_NAMES)))
def test_a_pure_ink_image_renders_entirely_as_that_ink(i, name, lut):
    a = np.full((48, 48, 3), ep.SPECTRA6_DITHER_PALETTE[i], dtype=np.uint8)
    for mode in ("linear", "legacy"):
        idx = ed.dither(a, mode=mode, lut=lut if mode == "linear" else None)
        assert (idx == i).all(), f"{mode}: pure {name} did not render as {name}"


def test_wavefront_ordering_is_a_valid_topological_order():
    """The whole vectorisation rests on k = 2y + x being a topological order of the FS dependency
    graph. Asserted directly rather than trusted: every source a pixel reads from must have a
    strictly smaller k, or pixels within a wavefront are not independent and the result is silently
    order-dependent."""
    for dy, dx, _ in ed._FS:
        # a pixel at (y,x) scatters to (y+dy, x+dx); that target must come strictly LATER
        assert 2 * dy + dx > 0, f"offset ({dy},{dx}) does not increase k=2y+x"


def test_nearest_ink_lut_maps_each_ink_to_itself(lut):
    inks_lin = ec.xyz_to_linear_rgb(pm.ink_xyz())
    got = ed._lut_lookup(lut, np.clip(inks_lin, 0.0, 1.0))
    assert (got == np.arange(6)).all(), f"LUT misclassifies its own inks: {got}"
