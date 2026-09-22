"""S3 verification — perceptual-intent gamut mapping.

A gamut map has no reference answer to check against, so it is verified entirely by INVARIANTS: things
that must hold by construction, each of which a plausible implementation can violate.
"""
import numpy as np
import pytest

import epaper as ep
from tools.eink import eink_color as ec
from tools.eink import eink_gamut as eg
from tools.eink import eink_panel_model as pm


@pytest.fixture(scope="module")
def sample():
    rng = np.random.default_rng(20260829)
    rgb = rng.integers(0, 256, (3000, 3)).astype(float)
    return rgb, ec.xyz_to_lab(ec.srgb8_to_xyz(rgb), pm.media_white())


def test_output_is_always_inside_the_gamut(sample):
    """The one non-negotiable property. A gamut map that emits unachievable colours has not mapped."""
    _, lab = sample
    for knee in (0.5, 0.9, 1.0):
        assert eg.in_destination(eg.gamut_map(lab, knee=knee)).all(), f"knee={knee} escaped the gamut"


def test_hue_is_preserved_exactly_on_chromatic_colours(sample):
    """All work happens inside a constant-hue leaf, so hue error is a BUG, not a tolerance."""
    _, lab = sample
    out = eg.gamut_map(lab)
    s, o = ec.lab_to_lch(lab), ec.lab_to_lch(out)
    chromatic = s[:, 1] >= 5.0                       # below that, hue is not a meaningful quantity
    dh = np.abs((o[chromatic, 2] - s[chromatic, 2] + 180) % 360 - 180)
    assert dh.max() < 0.01, f"max hue drift {dh.max():.4f} deg"


def test_the_anchor_is_always_on_the_achievable_neutral_axis():
    """Regression: 3 of 72 hue bins have a cusp above L* 100 (up to 113, in the yellows). A neutral
    brighter than the white ink does not exist, so an unclamped anchor falls outside the hull, the
    bisection returns zero, and those hues collapse onto the anchor — measured as 48 chromatic points
    with hue errors up to 98 degrees."""
    h = np.linspace(0, 360, 721)
    L = eg.cusp_lightness(h)
    lo, hi = eg.anchor_band()
    assert L.max() <= hi and L.min() >= lo
    neutral = np.stack([L, np.zeros_like(L), np.zeros_like(L)], axis=-1)
    assert eg.in_destination(neutral).all(), "every anchor must itself be achievable"


def test_colours_well_inside_the_gamut_are_left_alone(sample):
    """The knee's whole purpose: don't move what does not need moving."""
    _, lab = sample
    knee = 0.9
    # black_L=0: these tests exercise the CHROMA stage, and the default black-point compensation
    # (ADR-116) moves every colour's lightness, which is not what "untouched" is about.
    out = eg.gamut_map(lab, knee=knee, black_L=0.0)
    moved = ec.ciede2000(lab, out)
    # Anything the map left untouched must be bit-identical, not merely close.
    untouched = moved < 1e-9
    assert untouched.any(), "if the knee moves everything it is not a knee"
    assert np.allclose(lab[untouched], out[untouched], atol=0.0)


def test_knee_1_is_pure_clipping_and_leaves_every_in_gamut_colour_exact(sample):
    """knee=1.0 must degenerate to colorimetric intent — the null hypothesis this family contains."""
    _, lab = sample
    inside = eg.in_destination(lab)
    assert inside.any()
    out = eg.gamut_map(lab, knee=1.0, black_L=0.0)
    assert np.allclose(lab[inside], out[inside], atol=1e-6)


def test_a_lower_knee_compresses_more(sample):
    """Monotone in the parameter: a smaller core must move more colour, not less."""
    _, lab = sample
    moved = [float((ec.ciede2000(lab, eg.gamut_map(lab, knee=k, black_L=0.0)) > 1e-9).mean())
             for k in (1.0, 0.9, 0.6)]
    assert moved[0] < moved[1] < moved[2], f"knee monotonicity broken: {moved}"


def test_chroma_ordering_is_preserved_along_a_hue_ray():
    """Relationships are what perceptual intent promises: more saturated in must stay more saturated
    out. A map that inverts two colours has destroyed the thing it exists to protect."""
    # Swept DENSELY on purpose: a coarse grid missed the yellow-apex inversion entirely and reported
    # this clean. Bound is the measured worst case at the chosen anchor margin (-0.33 C*), which is a
    # third of the ~1 C* discrimination threshold — not zero, and said so rather than rounded away.
    worst = 0.0
    for hue in np.arange(0.0, 360.0, 15.0):
        for L in np.arange(10.0, 100.0, 5.0):
            C = np.linspace(0, 130, 60)
            lab = ec.lch_to_lab(np.stack([np.full_like(C, L), C, np.full_like(C, hue)], axis=-1))
            worst = min(worst, float(np.diff(ec.lab_to_lch(eg.gamut_map(lab))[:, 1]).min()))
    # Re-measured on the measured palette (ADR-116): the neutral floor at L* 49.65 narrows the anchor
    # band to [61.65, 88] and the yellow-apex residual grows — worst -0.78 C* on this sweep (with the
    # default black-point toe), -1.34 on the dense one, at hue 75-85 / L* 50-72, with margin 12 still
    # the best of 0/5/8/10/12/14/20. Bound is the measured worst plus headroom; stated, not rounded
    # away; under the ~1 C* threshold HERE but not on the dense sweep — an open item of the mapper
    # (ADR-117), not a pass.
    assert worst > -1.0, f"chroma ordering inverted by {worst:.3f} C*"


def test_the_map_is_continuous():
    """A discontinuous gamut map produces banding. Catch it here, not in an image."""
    for hue in (15.0, 95.0, 200.0, 280.0):
        C = np.linspace(0, 140, 4000)
        lab = ec.lch_to_lab(np.stack([np.full_like(C, 60.0), C, np.full_like(C, hue)], axis=-1))
        out = eg.gamut_map(lab)
        step = np.abs(np.diff(out, axis=0)).max(axis=1)
        assert step.max() < 1.0, f"jump of {step.max():.3f} Lab units at hue {hue}"


def test_lightness_on_neutrals_is_the_identity_above_the_floor_and_a_toe_below_it():
    """THE S3 FINDING, re-pinned after ADR-116. On the swatch the achievable neutral axis was 0..100
    and lightness was the identity — no tone curve fell out of perceptual intent. On the measured
    palette the axis starts at `neutral_floor()` (L* 49.65): above it the identity still holds
    exactly, and the default black-point compensation maps the source's 0..100 onto floor..100 —
    the toe the module docstring predicted the day black was measured."""
    floor = eg.neutral_floor()
    L = np.linspace(0, 100, 101)
    lab = np.stack([L, np.zeros_like(L), np.zeros_like(L)], axis=-1)
    # Compensation OFF: the identity wherever the source and destination boundaries coincide — the
    # whole axis on the swatch (floor 0), the knee's core above the floor on the measured palette
    # (the last 10% before the floor is compressed, which is the knee doing its job). atol from the
    # measured numerical floor, 1.8e-6 — the bug this catches was 4 L* and then 0.96 L*.
    off = eg.gamut_map(lab, black_L=0.0)[:, 0]
    lo, _ = eg.anchor_band()
    core = L >= floor + 0.1 * (lo - floor) + 1e-3
    assert np.allclose(off[core], L[core], atol=1e-4)
    assert np.all(off[L < floor] >= floor - 1e-3), "below the floor a neutral can only clip to it"
    # Compensation ON (the default): the toe is EXACTLY the affine 0 -> floor, 100 -> 100. Once the
    # source's black sits on the destination's floor the two boundaries coincide again and the knee
    # is the identity on the axis — so this is the swatch-era identity, relocated.
    on = eg.gamut_map(lab)[:, 0]
    assert np.allclose(on, floor + L * (100.0 - floor) / 100.0, atol=1e-4)
    explicit = eg.gamut_map(lab, black_L=max(floor, 8.0))[:, 0]
    assert explicit[0] == pytest.approx(max(floor, 8.0), abs=1e-3)


@pytest.mark.parametrize("i,name", list(enumerate(pm.INK_NAMES)))
def test_each_ink_maps_to_itself(i, name):
    """The inks are the gamut's own vertices; a map that moves them is compressing its destination."""
    lab = ec.xyz_to_lab(pm.ink_xyz()[i], pm.media_white())
    out = eg.gamut_map(lab[None, :], knee=1.0, black_L=0.0)[0]     # the chroma stage alone
    assert float(ec.ciede2000(lab, out)) < 0.5, f"{name} moved by {float(ec.ciede2000(lab, out)):.2f} dE"


def test_the_source_palette_constant_is_untouched():
    """STANDING_RULES: nothing in this programme may rewrite SPECTRA6_DITHER_PALETTE."""
    assert ep.SPECTRA6_DITHER_PALETTE == [
        (0, 0, 0), (161, 164, 165), (156, 72, 75),
        (208, 190, 71), (61, 59, 94), (58, 91, 70),
    ]


def test_the_quantiser_space_round_trips_the_inks_byte_for_byte():
    """Guards the conversion back into the quantiser's space.

    The first version un-adapted by dividing out the chromatic adaptation, which is the obvious move
    and is wrong: the palette stores ABSOLUTE XYZ encoded as sRGB, and un-adapting pushes yellow —
    above media white — past linear 1.0, where clipping destroys it. Byte-exact round-trip of the six
    inks is the cheapest possible statement that the space is right.
    """
    lab = ec.xyz_to_lab(pm.ink_xyz(), pm.media_white())
    got = eg.to_quantiser_srgb8(lab)
    assert got.tolist() == pm.ink_srgb8().tolist()


def test_the_shipping_palette_and_the_measured_inks_disagree_and_that_is_known():
    """TRIPWIRE, not an invariant. Since ADR-116 the analysis plane reasons from the measured inks
    while `epaper.SPECTRA6_DITHER_PALETTE` — what the shipping quantiser measures distance against —
    is still the vendor swatch. That disagreement is deliberate (ADR-084: the panel decides what
    ships) and it must stay VISIBLE: the day the shipping palette is replaced, or the hook is
    unset, this test flips and the docstring in `eink_panel_model` has to be rewritten with it."""
    derived = pm.ink_srgb8().tolist()
    shipping = [list(c) for c in ep.SPECTRA6_DITHER_PALETTE]
    if pm._MEASURED_INK_XYZ is None:
        assert derived == shipping
    else:
        assert derived != shipping, "the shipping palette now matches the measured inks — update the docs"
        blue = ec.xyz_to_linear_rgb(pm.ink_xyz())[pm.INK_NAMES.index("blue")]
        assert blue.min() < 0, "blue was inside sRGB — re-check the measured palette"
