"""S1 verification — the panel's geometry, checked by identities rather than by agreement with us.

Every test here can fail. Several are structural (Euler, vertex-ness, ray/hull identities) and hold for
any palette, so they keep working if a colorimeter ever replaces `SPECTRA6_DITHER_PALETTE`.
"""
import numpy as np
import pytest

from tools import eink_color as ec
from tools import eink_panel_model as pm


def test_hull_is_a_closed_polytope():
    faces, N, D = pm.hull_faces()
    edges = pm.hull_edges(faces)
    # Euler's formula. A hull routine that silently drops a face fails this and nothing else would.
    assert len(faces) - len(edges) + 6 == 2, f"V-E+F = {6 - len(edges) + len(faces)}, must be 2"
    assert len(faces) == 8 and len(edges) == 12
    assert np.allclose(np.linalg.norm(N, axis=1), 1.0)


def test_every_ink_is_a_hull_vertex():
    """No ink may lie inside the hull of the other five.

    If one ever does it is redundant — the dither could reach it by mixing — and that is a finding
    about the palette, not a test failure to paper over.
    """
    P = ec.xyz_to_linear_rgb(pm.ink_xyz())
    for i in range(6):
        others = np.delete(P, i, axis=0)
        _, N, D = pm.hull_faces(others)
        inside = np.all(P[i] @ N.T - D <= -1e-9)
        assert not inside, f"ink {pm.INK_NAMES[i]} is inside the hull of the other five"


def test_all_inks_are_inside_their_own_hull_and_absurd_colours_are_not():
    P = ec.xyz_to_linear_rgb(pm.ink_xyz())
    assert pm.hull_contains(P).all()
    # Pure saturated sRGB primaries are far outside a 1.11%-of-the-cube gamut.
    outside = ec.srgb_to_linear(np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 1]], dtype=float))
    assert not pm.hull_contains(outside).any()


def test_ray_to_a_vertex_lands_on_that_vertex():
    P = ec.xyz_to_linear_rgb(pm.ink_xyz())
    c = P.mean(axis=0)
    for i, name in enumerate(pm.INK_NAMES):
        d = P[i] - c
        t, hit = pm.hull_intersect(c, d)
        assert np.allclose(hit, P[i], atol=1e-12), f"ray toward {name} missed it by {np.abs(hit - P[i]).max()}"
        assert t == pytest.approx(1.0, abs=1e-12)


def test_ray_to_a_face_centroid_satisfies_that_faces_plane():
    P = ec.xyz_to_linear_rgb(pm.ink_xyz())
    faces, N, D = pm.hull_faces()
    c = P.mean(axis=0)
    for (i, j, k), n, d in zip(faces, N, D):
        centroid = P[[i, j, k]].mean(axis=0)
        _, hit = pm.hull_intersect(c, centroid - c)
        assert float(hit @ n - d) == pytest.approx(0.0, abs=1e-12)
        assert np.allclose(hit, centroid, atol=1e-12)


def test_sampled_cusp_matches_an_independent_monte_carlo():
    """The cusp table is a dense SAMPLE, not a closed form — Lab is nonlinear in XYZ, so hull edges
    are curves and a constant-hue leaf does not cut them algebraically. This bounds the sampling error
    against an independently drawn point set.
    """
    rng = np.random.default_rng(20260829)
    P = ec.xyz_to_linear_rgb(pm.ink_xyz())
    faces, _, _ = pm.hull_faces()
    w = rng.dirichlet(np.ones(3), size=(40000,))
    pts = np.concatenate([w @ P[list(t)] for t in faces], axis=0)
    lch = ec.lab_to_lch(ec.xyz_to_lab(ec.linear_rgb_to_xyz(pts), pm.media_white()))

    bins = 72
    idx = np.minimum((lch[:, 2] / 360.0 * bins).astype(int), bins - 1)
    table = {r["hue_deg"]: r["C_max"] for r in pm.cusp_table(bins=bins)}
    worst = 0.0
    for b in range(bins):
        m = idx == b
        if m.sum() < 50:
            continue
        mc = float(lch[m, 1].max())
        got = table.get(round(b * 360.0 / bins, 1))
        # Tolerance set FROM the convergence measurement (0.60 C* at n=96, 0.20 at n=160 and
        # unchanged at n=256, i.e. the Monte-Carlo's own floor), not lowered until it passed.
        assert got is not None and got >= mc - 0.35, f"hue bin {b}: table {got} < monte-carlo {mc}"
        worst = max(worst, abs(got - mc))
    assert worst < 1.0, f"sampled cusp disagrees with monte-carlo by {worst:.2f} C*"


def test_media_relative_lut_is_monotone_and_hits_the_paper_white():
    lut = pm.media_relative_lut()
    assert np.all(np.diff(lut) >= 0), "a tone transform that is not monotone reorders tones"
    assert lut[0] == 0
    # d=255 must land exactly on the panel's white ink encoded value.
    assert lut[255] == pytest.approx(round(255 * float(ec.linear_to_srgb(pm.media_white()[1]))), abs=1)


def test_the_white_point_is_a_curve_not_a_scale():
    """If it were a scale, e(d)/d would be constant. It is not, and that is the finding."""
    lut = pm.media_relative_lut()
    ratio = lut[8:] / np.arange(8, 256)
    assert ratio.max() - ratio.min() > 0.15, "the ratio must vary materially across the range"
    # ⚠️ srgb_encode(Y_white), NOT Y_white**(1/2.4). An earlier version asserted the latter and was
    # refuted here: encode(y) = 1.055*y**(1/2.4) - 0.055, and dropping the affine terms overstates the
    # derived white point by 0.02 (0.660 vs the correct 0.641).
    assert ratio[-1] == pytest.approx(float(ec.linear_to_srgb(pm.media_white()[1])), abs=0.005)
    assert ratio[-1] != pytest.approx(float(pm.media_white()[1]) ** (1 / 2.4), abs=0.005)


@pytest.fixture
def swatch(monkeypatch):
    """ADR-094/096's registered claims were made ABOUT the vendor swatch; they stay pinned on it."""
    monkeypatch.setattr(pm, "_MEASURED_INK_XYZ", None)


def test_measured_palette_restates_the_structure(swatch_free=None):
    """What ADR-116 changes structurally, pinned so a future palette edit is a visible event:
    yellow and white are luminance EQUALS (within the fit, not a 38% gap), and the largest gap on the
    media-relative lightness ladder is at the TOP (green -> white), not in the shadows."""
    if pm._MEASURED_INK_XYZ is None:
        pytest.skip("hook unset")
    s = pm.starvation_report()
    Y = {r["ink"]: r["Y"] for r in pm.ink_table()}
    assert abs(Y["yellow"] / Y["white"] - 1.0) < 0.06
    assert s["L_media"]["largest_gap"]["from"] == "green"
    assert s["L_media"]["largest_gap"]["to"] == "white"
    assert pm.media_floor_level() > 0.0


def test_luminance_reorders_the_inks_versus_the_flat_mean(swatch):
    """ADR-094's founding claim, pinned ON THE SWATCH. The flat mean puts white on top; real luminance puts yellow."""
    s = pm.starvation_report()
    assert s["flat_rgb_mean"]["top_ink"] == "white"
    assert s["linear_Y"]["top_ink"] == "yellow"
    assert s["L_media"]["top_ink"] == "yellow"
    # Shadow starvation SURVIVES the correction; it is the largest gap in a perceptual space.
    assert s["L_media"]["largest_gap"]["from"] == "black"
    assert s["L_media"]["largest_gap"]["to"] == "blue"
    assert s["L_media"]["largest_gap"]["pct_of_range"] > 30


def test_gamma_space_dither_error_matches_the_registered_prediction(swatch):
    """Registered BEFORE running, ON THE SWATCH: positive everywhere below the media ceiling (sRGB
    encoding is concave, so the realised mean radiance exceeds what the encoded arithmetic asserts),
    largest where the EOTF curvature is largest, and vanishing at black."""
    rep = pm.dither_error_report()
    p = rep["prediction_holds"]
    assert p["positive_everywhere_below_ceiling"]
    assert p["zero_at_black"]
    assert p["peak_is_in_the_shadows"]
    assert p["peak_error_L"] > 5.0, "if the defect were negligible there would be nothing to fix"
    assert p["floor_clipped_levels"] == 0


def test_gamma_space_dither_error_on_the_measured_palette():
    """The same prediction re-registered for a real black (ADR-116): still positive everywhere and
    peaking in the shadows, but it CANNOT vanish at the dark end — the first achievable level above
    the black ink still needs a mixture — and the levels below the black ink are floor-clipped, a
    gamut fact reported separately, exactly as the ceiling already was."""
    if pm._MEASURED_INK_XYZ is None:
        pytest.skip("hook unset")
    rep = pm.dither_error_report()
    p = rep["prediction_holds"]
    assert p["positive_everywhere_below_ceiling"]
    assert p["peak_is_in_the_shadows"]
    assert p["peak_error_L"] > 5.0
    assert p["floor_clipped_levels"] >= 1
    assert rep["floor_clipping_below"][0]["error_L"] == pytest.approx(
        float(ec.xyz_to_lab(pm.ink_xyz()[0], pm.media_white())[0]), abs=0.1), \
        "at d=0 the whole error is the black ink's own lightness"
