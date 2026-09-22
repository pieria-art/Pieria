"""
tests/test_eink_h121.py — H-121 (ADR-120's registered next hypothesis), tools/eink/eink_h121.py.

H-121's entire claim is that a bounded-residual or clustered-mask realisation keeps the exact LOCAL
MEAN of `eink_barycentric.decompose_clipped`'s weights while losing the per-pixel IGN grain the panel
judged 6/6 against (ADR-120). Every number asserted below was MEASURED first (see the session that
built this file) so a threshold is a margin on an observed value, not a guess.
"""
import numpy as np
import pytest

from tools.eink import eink_barycentric as eba
from tools.eink import eink_color as ec
from tools.eink import eink_gamut as eg
from tools.eink import eink_h121 as h121
from tools.eink import eink_panel_model as pm

# Captured at COLLECTION time, before any test file's fixture has run — several sibling test files
# (test_eink_optimise.py, test_eink_scielab.py, test_eink_panel_model.py) monkeypatch
# `pm._MEASURED_INK_XYZ` to None (module-scoped, in some cases live for the whole file, not just one
# test) to pin themselves to the vendor swatch. Every number in THIS file was measured against the real
# ink data, so an autouse fixture below pins it back explicitly, per test, rather than trusting that a
# prior module's hook has already been torn down by the time collection order reaches us.
_ORIGINAL_MEASURED_INK_XYZ = pm._MEASURED_INK_XYZ


@pytest.fixture(autouse=True)
def _pin_measured_inks(monkeypatch):
    """Make this file's tests immune to `_MEASURED_INK_XYZ` leaking in from another module, and
    guarantee (via `monkeypatch`, function-scoped, always reverted) that this file never becomes the
    leak for whatever runs after it."""
    monkeypatch.setattr(pm, "_MEASURED_INK_XYZ", _ORIGINAL_MEASURED_INK_XYZ)


def _gradient_image(h=128, w=128):
    """A smoothly-varying synthetic field — no two neighbouring pixels share a target weight vector,
    so a real local-mean claim (not a flat-field coincidence) is under test."""
    xs = np.linspace(0, 255, w)
    ys = np.linspace(0, 255, h)
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[..., 0] = xs[None, :]
    img[..., 1] = ys[:, None]
    img[..., 2] = (xs[None, :] + ys[:, None]) / 2
    return img


# --- Shape / range invariants, both arms ------------------------------------------------------------

def test_both_arms_produce_valid_index_maps():
    img = _gradient_image()
    idx_c = h121.dither_residual(img)
    idx_d = h121.dither_clustered(img)
    for idx in (idx_c, idx_d):
        assert idx.dtype == np.uint8
        assert idx.shape == img.shape[:2]
        assert set(np.unique(idx).tolist()) <= {0, 1, 2, 3, 4, 5}


def test_both_arms_preview_in_exactly_the_measured_ink_colours():
    img = _gradient_image()
    allowed = {tuple(row) for row in pm.ink_srgb8().tolist()}
    inks = pm.ink_srgb8()
    for idx in (h121.dither_residual(img), h121.dither_clustered(img)):
        preview = inks[idx.astype(np.int64)].reshape(-1, 3)
        assert {tuple(px) for px in np.unique(preview, axis=0).tolist()} <= allowed


# --- The falsifiable claim: local mean --------------------------------------------------------------

def test_local_mean_survives_for_residual_diffusion():
    """Arm C. Measured on this gradient at block=16: 0.21 ink-fraction points (vs 0.22 for the B
    control, i.e. no worse than per-pixel IGN — the point is that it is not WORSE, not that it beats
    an already-unbiased sampler). Asserted with headroom."""
    img = _gradient_image()
    idx = h121.dither_residual(img)
    err = h121.local_mean_error(img, idx, block=16)
    assert err < 0.6, f"H-121 arm C's local-mean claim failed: {err:.3f} ink-fraction points"


def test_local_mean_survives_for_clustered_mask():
    """Arm D. Measured on this gradient at block=16: 0.63 points, falling to ~0.13 at block=64 (the
    ordered-dither residual averages out over a window covering more of the tile's period) — larger
    than C's because the realisation is still a threshold sample, just against a coarser mask."""
    img = _gradient_image()
    idx = h121.dither_clustered(img, tile=16)
    err = h121.local_mean_error(img, idx, block=64)
    assert err < 1.0, f"H-121 arm D's local-mean claim failed: {err:.3f} ink-fraction points"


def test_local_mean_error_shrinks_with_window_size():
    """H-121's claim is a LOCAL-mean claim, not a per-pixel one — so the residual error must fall as
    the averaging window grows, for both arms, on the same field."""
    img = _gradient_image(256, 256)
    idx_c = h121.dither_residual(img)
    idx_d = h121.dither_clustered(img, tile=16)
    for idx in (idx_c, idx_d):
        small = h121.local_mean_error(img, idx, block=8)
        large = h121.local_mean_error(img, idx, block=64)
        assert large < small, "local-mean error must shrink at a larger window if the claim is real"


# --- Boundedness: the reason ADR-108's blow-up cannot recur ----------------------------------------

def test_residual_diffusion_does_not_reproduce_the_adr108_blowup():
    """ADR-108's control: a neutral field beside an out-of-gamut region. Classic RGB Floyd-Steinberg
    carries an error that can point outside the gamut and never discharge (E Ink US11721296B2); this
    reproduces that scene against arm C and asserts the neutral region stays close to neutral — the
    residual here is a difference of two points on the 6-simplex, bounded by construction, so it cannot
    develop the unbounded colour cast that sank linear-light FS on Café Terrace (a magenta band on the
    awning, ADR-108/117).

    ⚠️ Feeding raw sRGB straight to `dither_residual` is not the right control: an untouched sRGB
    (128,128,128) is neutral relative to D65, not relative to the panel's own measured white, so it
    decomposes with a small inherent cast (measured a*≈4.6) that has nothing to do with diffusion —
    exactly ADR-120's "the real black carries chroma" finding. Production always gamut-maps first
    (`eink_gamut.map_srgb8` / `to_quantiser_srgb8`, as `eink_candidate.variant_b_index` does), which
    puts a true neutral at a*=b*=0 before decomposition ever sees it — so that is what this test feeds
    arm C, exactly like the existing `test_eink_candidate` neutral-field test does for arm B."""
    h, w = 64, 96
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, : w // 2] = (128, 128, 128)              # neutral half
    img[:, w // 2:] = (255, 0, 255)                 # saturated, likely out-of-gamut, half
    q = eg.to_quantiser_srgb8(eg.map_srgb8(img))

    idx = h121.dither_residual(q)
    neutral_idx = idx[:, : w // 2]
    inks_lin = ec.xyz_to_linear_rgb(pm.ink_xyz())
    realised = inks_lin[neutral_idx].reshape(-1, 3).mean(axis=0)
    lab = ec.xyz_to_lab(ec.linear_rgb_to_xyz(realised), pm.media_white())
    assert abs(lab[1]) < 1.0 and abs(lab[2]) < 1.0, (
        f"neutral half picked up a colour cast from its out-of-gamut neighbour: "
        f"a*={lab[1]:.2f} b*={lab[2]:.2f}")


# --- Decision frequency: what D actually buys, and what C does not ---------------------------------

def test_clustered_mask_lowers_decision_frequency_versus_per_pixel_ign():
    """This is the number behind H-121's 'texture rather than snow' bet for arm D: on a flat mixed-ink
    field (the worst case for grain — no image content to hide it in), the clustered mask changes ink
    far less often between neighbouring pixels than per-pixel IGN. Measured: B 0.92, D 0.21."""
    flat = np.full((128, 128, 3), (150, 120, 90), dtype=np.uint8)
    idx_b = eba.dither(flat)
    idx_d = h121.dither_clustered(flat, tile=16)
    freq_b = h121.decision_frequency(idx_b)
    freq_d = h121.decision_frequency(idx_d)
    assert freq_d < freq_b - 0.3, (
        f"arm D should clearly lower decision frequency vs per-pixel IGN: B={freq_b:.3f} D={freq_d:.3f}")


def test_residual_diffusion_does_not_measurably_lower_decision_frequency():
    """Named here so it cannot be silently claimed later: on the same flat field, arm C's raw
    per-pixel toggle rate is (measured) 0.89 against B's 0.92 — barely different. Error diffusion's
    known benefit over independent threshold sampling is CORRELATION structure (it pushes error to a
    blue-noise spectrum instead of a white-noise one), not a lower raw toggle count; this test pins
    that so a future report does not credit arm C with a frequency reduction its mechanism does not
    produce. See the report for the honest framing of this."""
    flat = np.full((128, 128, 3), (150, 120, 90), dtype=np.uint8)
    idx_b = eba.dither(flat)
    idx_c = h121.dither_residual(flat)
    freq_b = h121.decision_frequency(idx_b)
    freq_c = h121.decision_frequency(idx_c)
    assert abs(freq_c - freq_b) < 0.1, (
        f"if this fails, arm C's toggle rate moved more than expected: B={freq_b:.3f} C={freq_c:.3f}")


# --- Clustered-mask construction ---------------------------------------------------------------------

def test_clustered_mask_is_a_permutation_covering_the_unit_interval():
    m = h121.clustered_mask(n=8)
    vals = np.sort(m.ravel())
    expected = (np.arange(64) + 0.5) / 64.0
    assert np.allclose(vals, expected), "every rank 0..n*n-1 must appear exactly once"
    seed_y, seed_x = np.unravel_index(np.argmin(m), m.shape)
    assert abs(seed_y - 3.5) <= 1 and abs(seed_x - 3.5) <= 1, \
        "the smallest rank (the dot's seed) must sit at the tile centre, not at a corner"


def test_dither_clustered_matches_dither_with_the_same_mask():
    """Arm D is documented as reusing `eink_barycentric.dither`'s own realisation mechanism, just with
    a different noise field — assert that identity directly rather than trusting the docstring."""
    img = _gradient_image(32, 48)
    tile = 8
    from tools.eink.eink_h121 import _tile_mask
    noise = _tile_mask(h121.clustered_mask(tile), img.shape[0], img.shape[1]).reshape(-1)
    want = eba.dither(img, noise=noise)
    got = h121.dither_clustered(img, tile=tile)
    assert np.array_equal(want, got)
