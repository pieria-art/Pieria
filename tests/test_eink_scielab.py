"""S4 verification — the spatial perceptual objective.

The single most important test in the file is `test_degenerate_renders_lose_to_real_ones`. The
objective ADR-097 withdrew failed exactly that: it preferred a grey rectangle to the picture, because
rewarding the absence of a failure is not the same as rewarding a good image. It costs seconds to run
and it is the check that would have caught two months of work in twenty minutes.
"""
import numpy as np
import pytest
from PIL import Image

import epaper as ep
from tools import eink_color as ec
from tools import eink_dither as ed
from tools import eink_panel_model as pm
from tools import eink_scielab as sl

_LUT = ed.nearest_ink_lut()


def _quantize(rgb8):
    """The PRODUCTION quantiser — what actually ships, so what the objective must judge."""
    pal = ep._cached_palette_image("_spectra6_dither", ep.SPECTRA6_DITHER_PALETTE)
    im = Image.fromarray(np.asarray(rgb8, dtype=np.uint8), "RGB")
    return np.asarray(im.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG))


def _media_xyz(rgb8):
    """Source in media-relative terms: the reference the objective scores against."""
    w = pm.media_white() / ec.D65
    return ec.srgb8_to_xyz(np.asarray(rgb8, dtype=np.float64)) * w


def test_reduces_to_plain_cielab_on_a_flat_patch():
    """One test, four bugs: DC gain, weight renormalisation, the opponent round-trip, and the filter
    sign. On a spatially constant image there is nothing for a spatial filter to do, so S-CIELAB MUST
    equal ordinary CIELAB exactly."""
    a = np.broadcast_to(ec.srgb8_to_xyz([120, 130, 140]), (64, 64, 3)).copy()
    b = np.broadcast_to(ec.srgb8_to_xyz([128, 120, 150]), (64, 64, 3)).copy()
    plain = float(ec.ciede2000(ec.xyz_to_lab(a[0, 0], pm.media_white()),
                               ec.xyz_to_lab(b[0, 0], pm.media_white())))
    for d in sl.DISTANCES_M:
        got = sl.difference(a, b, d)
        assert np.allclose(got, plain, atol=1e-9), f"at {d} m: {got.mean():.9f} vs {plain:.9f}"


def test_filter_preserves_the_mean():
    rng = np.random.default_rng(3)
    xyz = ec.srgb8_to_xyz(rng.integers(0, 256, (64, 96, 3)).astype(float))
    for d in sl.DISTANCES_M:
        out = sl.filter_opponent(xyz, d)
        assert np.allclose(out.reshape(-1, 3).mean(axis=0), xyz.reshape(-1, 3).mean(axis=0), atol=1e-9)


def test_transfer_function_has_unit_dc_gain():
    for c in range(3):
        for d in sl.DISTANCES_M:
            H = sl._transfer((64, 64), sl.pixels_per_degree(d), c)
            assert H[0, 0] == pytest.approx(1.0, abs=1e-12), f"channel {c} at {d} m: DC {H[0, 0]}"


def test_opponent_round_trip_is_the_identity():
    rng = np.random.default_rng(11)
    xyz = rng.uniform(0, 1, (32, 32, 3))
    assert np.allclose((xyz @ sl._XYZ_TO_OPP.T) @ sl._OPP_TO_XYZ.T, xyz, atol=1e-12)


def test_grain_fades_with_distance_but_tone_error_does_not():
    """Two stimuli whose correct behaviour is known a priori, so the test can fail.

    A dithered flat grey differs from its reference ONLY in high-frequency pattern, so its penalty must
    fall monotonically as the viewer backs away and the pattern fuses. A smooth gradient rendered with
    a constant tone offset differs only at LOW frequency, so its penalty must be near distance-invariant.
    """
    # ⚠️ THE STIMULUS MUST BE A CORRECT RENDER, and an earlier version of this test got that wrong.
    # A flat grey through the PRODUCTION path differs from its reference in MEAN as well as in
    # pattern — by the S2 defect — so its penalty is ~13 dE00 and barely moves with distance, and the
    # test read as "grain does not fade". It is tone, not grain. Isolating grain needs a render whose
    # mean already matches: the exact media-relative transform plus the linear-light dither.
    flat = np.full((192, 192, 3), 110, dtype=np.uint8)
    exact = pm.media_relative_lut().astype(np.uint8)[flat]
    ren = sl.ink_field_xyz(ed.dither(exact, mode="linear", lut=_LUT))
    grain = [sl.difference(ren, _media_xyz(flat), d).mean() for d in sl.DISTANCES_M]
    assert all(x > y for x, y in zip(grain, grain[1:])), f"grain must fade with distance: {grain}"
    assert grain[0] / grain[-1] > 1.5, f"and fade materially: {grain[0]:.2f} -> {grain[-1]:.2f}"

    ramp = np.tile(np.linspace(40, 200, 192, dtype=np.uint8)[None, :, None], (192, 1, 3))
    off = np.clip(ramp.astype(int) + 12, 0, 255).astype(np.uint8)
    tone = [sl.difference(_media_xyz(off), _media_xyz(ramp), d).mean() for d in sl.DISTANCES_M]
    assert max(tone) / min(tone) < 1.10, f"a low-frequency offset must be ~distance-invariant: {tone}"


def test_degenerate_renders_lose_to_real_ones():
    """⛔ THE ONE THAT MATTERS. ADR-097's objective preferred a grey rectangle to the picture.

    Any objective that scores a constant image better than an honest render of the work is not
    measuring image quality, whatever its accuracy looks like. Run on real art, at every distance.
    """
    src = np.asarray(Image.open(
        "art-pack/_Library/dutch-golden-age__the-night-watch__ff740524.jpg"
    ).convert("RGB").resize((192, 144)))
    ref = _media_xyz(src)
    honest = sl.ink_field_xyz(_quantize(src))

    degenerates = {
        "flat mid-grey": np.broadcast_to(pm.ink_xyz()[pm.WHITE] * 0.5, honest.shape).copy(),
        "pure black": np.broadcast_to(pm.ink_xyz()[pm.BLACK], honest.shape).copy(),
        "pure white": np.broadcast_to(pm.ink_xyz()[pm.WHITE], honest.shape).copy(),
    }
    for d in sl.DISTANCES_M:
        good = sl.difference(honest, ref, d).mean()
        for name, deg in degenerates.items():
            bad = sl.difference(deg, ref, d).mean()
            assert bad > good, f"at {d} m the objective prefers {name} ({bad:.2f}) to the render ({good:.2f})"


def test_the_wide_negative_lobe_does_not_decide_anything():
    """The luminance channel's sigma3 = 4.336 deg exceeds the image at >= 2 m, so its value is set by
    the boundary condition. If dropping it reorders two candidates, the ordering was a padding
    artefact — so the check is that the RANKING survives, not that the numbers match."""
    src = np.asarray(Image.open(
        "art-pack/_Library/masterpieces__sunflowers__07310daa.jpg"
    ).convert("RGB").resize((160, 160)))
    ref = _media_xyz(src)
    cands = {"honest": sl.ink_field_xyz(_quantize(src)),
             "washed": sl.ink_field_xyz(_quantize(np.clip(src.astype(int) + 40, 0, 255).astype(np.uint8))),
             "flat": np.broadcast_to(pm.ink_xyz()[pm.WHITE] * 0.6, (160, 160, 3)).copy()}
    for d in (2.0, 3.0):
        full = {k: sl.difference(v, ref, d).mean() for k, v in cands.items()}
        trim = {k: sl.difference(v, ref, d, w3_zero=True).mean() for k, v in cands.items()}
        assert sorted(full, key=full.get) == sorted(trim, key=trim.get), \
            f"at {d} m the ranking depends on the boundary-dominated lobe: {full} vs {trim}"


def test_subsampling_the_difference_field_is_unbiased():
    """The compute saving must be a sample of the ANSWER, never a downsample of the IMAGE — the dither
    pattern is the signal. This bounds the error it introduces."""
    src = np.asarray(Image.open(
        "art-pack/_Library/impressionism__olympia__e9572d40.jpg"
    ).convert("RGB").resize((240, 240)))
    ref, ren = _media_xyz(src), sl.ink_field_xyz(_quantize(src))
    full = sl.worst_case(ren, ref, distances=(1.5,), stride=1)["objective"]
    sub = sl.worst_case(ren, ref, distances=(1.5,), stride=4)["objective"]
    assert abs(full - sub) < 0.05, f"stride-4 sample off by {abs(full - sub):.4f} dE"


def test_the_objective_prefers_the_corrected_pipeline_to_production():
    """A sanity floor on the whole programme: if the physics-derived pipeline did NOT score better
    than what ships, something upstream is wrong and no optimisation should be run.

    Both defects are priced separately on a flat grey, and neither is subtle (1 dE00 is roughly a
    just-noticeable difference):
        production (wp 0.75 + Pillow FS)   13.5 dE00
        exact e(d) + Pillow FS              8.4      <- the derived white point alone
        exact e(d) + linear-light FS        1.2      <- plus conserving the right quantity
    """
    flat = np.full((160, 160, 3), 110, dtype=np.uint8)
    ref = _media_xyz(flat)
    shipped = np.array(ep._tone_lut(ep.SPECTRA6_WHITE_POINT, ep.SPECTRA6_GAMMA), dtype=np.uint8)[flat]
    exact = pm.media_relative_lut().astype(np.uint8)[flat]

    production = sl.difference(sl.ink_field_xyz(_quantize(shipped)), ref, 1.5).mean()
    derived_wp = sl.difference(sl.ink_field_xyz(_quantize(exact)), ref, 1.5).mean()
    fully_fixed = sl.difference(sl.ink_field_xyz(ed.dither(exact, mode="linear", lut=_LUT)), ref, 1.5).mean()

    assert derived_wp < production, f"the derived white point must help: {derived_wp:.2f} vs {production:.2f}"
    assert fully_fixed < derived_wp, f"conserving radiance must help too: {fully_fixed:.2f}"
    assert fully_fixed < 3.0, f"the corrected pipeline should be near-exact on a flat patch: {fully_fixed:.2f}"
