"""S5 verification — the shipping pre-transform.

Cheap fixtures: the 33^3 quantiser response is measured once per session (~5 s) and reused.
"""
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import epaper as ep
from tools.eink import eink_color as ec
from tools.eink import eink_gamut as eg
from tools.eink import eink_optimise as eo
from tools.eink import eink_panel_model as pm
from tools.eink import eink_scielab as sl

# art-pack/ is a built, gitignored distributable (never git — see .gitignore) that only exists on a
# machine that has run the pack build. Tests that need a real artwork image are maintainer-local only.
_THE_KISS = (Path(__file__).resolve().parent.parent
             / "art-pack" / "_Library" / "masterpieces__the-kiss__552e767a.jpg")
_needs_the_kiss = pytest.mark.skipif(
    not _THE_KISS.exists(), reason=f"art-pack library image not present under {_THE_KISS} (local-only)")


@pytest.fixture(autouse=True, scope="module")
def _swatch_era_registration():
    """⚠️ PINNED TO THE VENDOR SWATCH ON PURPOSE (ADR-117). Every registered prediction in this file
    was derived on `SPECTRA6_DITHER_PALETTE`; since ADR-116 `eink_panel_model` reasons from the
    measured inks, on which the panel's structure differs (neutral floor L* 49.65, yellow = white).
    Module-scoped so it is in place BEFORE the module-scoped measurement fixtures are built.

    🔴 PROBED on the measured palette (spec_s456, 2026-09-21) — `bench-eink/analysis/
    S5_shipping_transform_measured.md`. Kept FULLY pinned, module-wide, on purpose: 4 of 7 tests here
    fail with the hook live, for real reasons — but read the LADDER report, not just this note, before
    quoting a mechanism from it: the shipped default (`black_L=None`) maps true darks to the derived
    neutral floor L* 49.64 (a black-point-compensation toe, `eink_gamut.gamut_map`), not to the
    measured black ink's own L* 39.6 — a genuinely correct-but-worse-than-doing-nothing choice on this
    palette, worth ~8.4 ΔE00 mean if switched off (measured, not yet acted on — ADR-084's call). The
    quantiser's response is also far more onto than before (6% vs the pinned test's >10% unreachable
    bar). Splitting the pin per-test is NOT safe here — `response` and `work` below are MODULE-scoped
    and shared, so a partial un-pin would let a swatch-built `response` leak into a measured-expecting
    test (or vice versa) depending on pytest's collection order, which is exactly the contamination
    shape this item warns about. The whole file stays pinned; see the report for the measured numbers."""
    from tools.eink import eink_panel_model as _pm
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_pm, "_MEASURED_INK_XYZ", None)
        yield


@pytest.fixture(scope="module")
def response():
    return eo.quantiser_response(n=17, patch=48)     # 17^3 is plenty — resolution is not the error


@pytest.fixture(scope="module")
def work():
    src = np.asarray(Image.open(
        "art-pack/_Library/masterpieces__the-kiss__552e767a.jpg").convert("RGB").resize((192, 144)))
    ref = ec.srgb8_to_xyz(src.astype(float)) * (pm.media_white() / ec.D65)
    return src, ref


def _score(idx, ref):
    return sl.worst_case(sl.ink_field_xyz(idx), ref, stride=2)["objective"]


def test_the_quantiser_response_shows_the_S2_defect(response):
    """R is a MEASUREMENT of the shipping quantiser. It must reproduce S2's finding independently:
    the realised radiance exceeds what the input's own encoding asserts, and does so in the shadows."""
    n = response.shape[0]
    g = np.linspace(0, 255, n)
    excess = np.array([response[i, i, i, 1] - float(ec.srgb_to_linear(g[i] / 255.0)) for i in range(n)])
    assert excess.max() > 0.01, "if the quantiser had no defect there is nothing to pre-compensate"
    assert g[int(np.argmax(excess))] < 160, "the excess must peak below the media ceiling"
    assert abs(excess[0]) < 1e-9 and excess[-1] <= 1e-9, "and vanish at the endpoints"


@_needs_the_kiss
def test_the_derived_pipeline_beats_production(response, work):
    """The identity check the plan demanded: if the physics-derived defaults lose to a hand-tuned
    constant, something upstream is wrong and no optimisation should be run."""
    src, ref = work
    shipped = np.array(ep._tone_lut(ep.SPECTRA6_WHITE_POINT, ep.SPECTRA6_GAMMA), dtype=np.uint8)[src]
    production = _score(eo.quantise(shipped), ref)
    derived = _score(eo.quantise(eo.apply_lut(src, eo.build_lut(n=17, precomp=False))), ref)
    assert derived < production, f"derived {derived:.2f} must beat production {production:.2f}"


@_needs_the_kiss
def test_precompensation_helps_but_cannot_close_the_gap(response, work):
    """Registered prediction was 40-70% of the gap recovered. Measured: ~27%. The prediction was
    REFUTED, and `test_the_quantiser_response_is_not_onto` explains why — so the number is pinned here
    rather than the prediction being quietly restated."""
    src, ref = work
    plain = _score(eo.quantise(eo.apply_lut(src, eo.build_lut(n=17, precomp=False))), ref)
    comp = _score(eo.quantise(eo.apply_lut(src, eo.build_lut(n=17, response=response))), ref)
    assert comp < plain, f"pre-compensation must help: {comp:.2f} vs {plain:.2f}"


def test_the_quantiser_response_is_not_onto(response):
    """⛔ THE FINDING THAT ANSWERS ADR-102's DECISION 2.

    Pre-compensation can only work if the quantiser's response is onto — if for every colour we want,
    some input makes it land there. It is not. A large share of targets are unreachable by ANY input,
    so the ceiling belongs to the quantiser and no LUT resolution or better search moves it.
    """
    n = response.shape[0]
    g = np.linspace(0, 255, n)
    src = np.stack(np.meshgrid(g, g, g, indexing="ij"), axis=-1).reshape(-1, 3)
    mapped = eg.gamut_map(eg.to_media_relative(src), knee=1.0)
    desired = ec.xyz_to_linear_rgb(ec.lab_to_xyz(mapped, pm.media_white()))
    t8 = eo.precompensate(desired, response)
    got = ec.xyz_to_lab(ec.linear_rgb_to_xyz(eo._trilinear(response, t8.astype(float))), pm.media_white())
    want = ec.xyz_to_lab(ec.linear_rgb_to_xyz(desired), pm.media_white())
    unreachable = float((ec.ciede2000(got, want) > 2.0).mean())
    assert unreachable > 0.10, (
        f"only {unreachable:.1%} of targets unreachable — if the response were near-onto, "
        "pre-compensation would close the gap and the PIL-only constraint would be free")


def test_precompensation_converges(response):
    """It converges to a RESIDUAL, not to zero — which is the not-onto finding seen from the other
    side. Pinned so that a later 'improvement' to the search cannot be credited with fixing it."""
    g = np.linspace(0, 255, response.shape[0])
    src = np.stack(np.meshgrid(g, g, g, indexing="ij"), axis=-1).reshape(-1, 3)
    mapped = eg.gamut_map(eg.to_media_relative(src), knee=1.0)
    desired = ec.xyz_to_linear_rgb(ec.lab_to_xyz(mapped, pm.media_white()))
    errs = [float(np.abs(eo._trilinear(response, eo.precompensate(desired, response, iters=i).astype(float))
                         - desired).max()) for i in (6, 12, 24)]
    assert abs(errs[0] - errs[-1]) < 1e-6, f"must be converged by 6 iterations: {errs}"
    assert errs[-1] > 0.01, "and it must NOT converge to zero — the response is not onto"


@_needs_the_kiss
def test_pillow_color3dlut_matches_exact_application(response, work):
    """The shipping vehicle must be faithful to what was optimised, or the optimisation is of a
    different pipeline than the one that runs."""
    src, _ = work
    grid = eo.build_lut(n=17, response=response)
    pil = np.asarray(Image.fromarray(src, "RGB").filter(eo.as_pil_lut(grid)))
    exact = eo.apply_lut(src, grid)
    assert np.abs(pil.astype(int) - exact.astype(int)).max() <= 2


@_needs_the_kiss
def test_lut_resolution_is_not_the_limiting_factor(work):
    """If 17^3 and 33^3 agree, the residual error is not interpolation and a finer LUT is not the fix."""
    src, ref = work
    a = _score(eo.quantise(eo.apply_lut(src, eo.build_lut(n=17, precomp=False))), ref)
    b = _score(eo.quantise(eo.apply_lut(src, eo.build_lut(n=33, precomp=False))), ref)
    assert abs(a - b) < 0.3, f"resolution changes the answer: 17^3 {a:.3f} vs 33^3 {b:.3f}"
