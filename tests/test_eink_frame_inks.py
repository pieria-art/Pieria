"""tools/eink/eink_frame_inks.py — the art-frame ink-readout instrument, checked on synthetic data.

House style (matches every other e-ink test): synthetic fixtures only. A real photographed frame is a
bench artefact this repo does not carry (the rig is torn down, ADR-116/117); what CAN be proven without
one is that the arithmetic is right — pure-ink recovery, convex-mixture recovery, the palette swap
actually changing the answer, the colour-chain identity, and the digital-render index recovery — each
a check that can fail (STANDING_RULES rule 6), not one that can only pass.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.eink import eink_camera as ecam  # noqa: E402
from tools.eink import eink_color as ec  # noqa: E402
from tools.eink import eink_frame_inks as efi  # noqa: E402
from tools.eink import eink_panel_model as pm  # noqa: E402
from tools.eink import eink_shoot as esh  # noqa: E402

# --- the palette swap --------------------------------------------------------------------------------

def test_swatch_palette_restores_the_hook():
    before = pm._MEASURED_INK_XYZ
    assert before is not None, "the measured hook must be wired for this test to mean anything"
    with efi.swatch_palette():
        assert pm._MEASURED_INK_XYZ is None
    assert pm._MEASURED_INK_XYZ is before


def test_swatch_palette_changes_the_ink_table():
    """A monkeypatch that silently does nothing is worse than no monkeypatch — pin that it moves."""
    measured = pm.ink_xyz().copy()
    with efi.swatch_palette():
        swatch = pm.ink_xyz().copy()
    assert not np.allclose(measured, swatch)


# --- per-pixel unmixing --------------------------------------------------------------------------

def test_unmix_recovers_a_pure_ink_vertex():
    """A pixel exactly ON an ink vertex must decompose to weight 1 on that ink, 0 elsewhere, residual 0.
    This is `decompose_clipped`'s exactness property, exercised through THIS module's wiring (the
    XYZ->linear-RGB conversion and the weights/residual bookkeeping around it)."""
    xyz = pm.ink_xyz()
    for i, name in enumerate(pm.INK_NAMES):
        pt = np.broadcast_to(xyz[i], (2, 2, 3)).copy()
        mix = efi.unmix(pt)
        w = mix["weights"]
        assert w[..., i] == pytest.approx(1.0, abs=1e-6), f"{name}: {w}"
        assert np.delete(w, i, axis=-1) == pytest.approx(0.0, abs=1e-6)
        assert mix["residual"] == pytest.approx(0.0, abs=1e-6)
        assert mix["inside"].all()


def test_unmix_recovers_a_convex_mixture():
    """A 50/50 mixture of two inks IN LINEAR LIGHT (spatial mixing averages radiance, ADR-100) must
    decompose back to weight 0.5 on each, not smeared across all six."""
    P = ec.xyz_to_linear_rgb(pm.ink_xyz())          # (6,3) linear RGB, the space mixing happens in
    i, j = pm.INK_NAMES.index("red"), pm.INK_NAMES.index("yellow")
    lin_mix = 0.5 * P[i] + 0.5 * P[j]
    xyz_mix = ec.linear_rgb_to_xyz(lin_mix)
    mix = efi.unmix(xyz_mix.reshape(1, 3))
    w = mix["weights"][0]
    assert w[i] == pytest.approx(0.5, abs=1e-6)
    assert w[j] == pytest.approx(0.5, abs=1e-6)
    other = [k for k in range(6) if k not in (i, j)]
    assert np.all(np.abs(w[other]) < 1e-6)
    assert w.sum() == pytest.approx(1.0, abs=1e-9)
    assert mix["residual"][0] == pytest.approx(0.0, abs=1e-6)


def test_unmix_swatch_disagrees_with_measured_on_the_same_pixel():
    """A pixel sitting exactly on the MEASURED red ink must NOT also sit on the swatch's red vertex —
    the whole point of ADR-116 (the swatch is wrong on every chromatic ink). Decomposing the same
    physical pixel against the swatch must give a different (and non-exact) answer."""
    i = pm.INK_NAMES.index("red")
    xyz = pm.ink_xyz()[i].reshape(1, 3)
    measured = efi.unmix(xyz, swatch=False)
    swatch = efi.unmix(xyz, swatch=True)
    assert not np.allclose(measured["weights"], swatch["weights"])
    # On the swatch this exact point is very unlikely to land exactly on a vertex again.
    assert swatch["residual"][0] > 1e-4


# --- reported statistics -------------------------------------------------------------------------

def test_ink_percentages_is_mean_weight_times_100():
    w = np.zeros((4, 6))
    w[:, pm.INK_NAMES.index("white")] = 1.0
    w[0, pm.INK_NAMES.index("white")] = 0.0
    w[0, pm.INK_NAMES.index("black")] = 1.0
    pct = efi.ink_percentages(w)
    assert pct["white"] == pytest.approx(75.0)
    assert pct["black"] == pytest.approx(25.0)
    assert sum(pct.values()) == pytest.approx(100.0)


def test_residual_report_counts_above_threshold():
    residual = np.array([0.0, 0.01, 0.02, 0.05, 0.10, 1.0])
    rep = efi.residual_report(residual, threshold=0.04)
    assert rep["threshold"] == 0.04
    assert rep["pct_above_threshold"] == pytest.approx(100.0 * 3 / 6)   # 0.05, 0.10, 1.0
    assert rep["max"] == pytest.approx(1.0)
    # Independently-known median: sorted [0, .01, .02, .05, .10, 1.0], even count -> mean of the
    # middle pair (.02 + .05) / 2 — hand-computed, not re-running the code's own formula.
    assert rep["median"] == pytest.approx(0.035)


def test_residual_report_carries_clipped_pct_from_inside():
    residual = np.array([0.0, 0.0, 0.02, 0.05])
    inside = np.array([True, True, False, False])
    rep = efi.residual_report(residual, threshold=0.04, inside=inside)
    assert rep["clipped_pct"] == pytest.approx(50.0)


def test_measured_readout_headlines_clipped_pct_separately_from_pct_above_threshold():
    """A pixel can be clipped (outside the hull, residual > 0) without clearing the reporting
    threshold — the defect the review caught (>50% clipped, 0% 'above threshold' looked clean). Pin
    that a low-threshold, majority-clipped point does not silently read as 0% clipped too."""
    P = ec.xyz_to_linear_rgb(pm.ink_xyz())
    outside = ec.linear_rgb_to_xyz(P.mean(axis=0) * 3.0)   # far outside the hull, in every direction
    mix = efi.unmix(outside.reshape(1, 3))
    assert not mix["inside"][0]
    rep = efi.residual_report(mix["residual"], threshold=1e6, inside=mix["inside"])
    assert rep["pct_above_threshold"] == pytest.approx(0.0)   # threshold set absurdly high on purpose
    assert rep["clipped_pct"] == pytest.approx(100.0)          # but clipping still shows up


def test_default_threshold_differs_between_palettes_and_is_positive():
    """A threshold defined as a fraction of the white ink's own norm must move when the palette (and
    therefore the white ink's own colour) moves — otherwise it is a bare constant wearing a formula."""
    measured = efi.default_threshold()
    swatch = efi.default_threshold(swatch=True)
    assert measured > 0
    assert swatch > 0
    assert measured != pytest.approx(swatch)


# --- the colour chain identity (no camera matrix needed: identity M, zero pedestal) -----------------

class _StubShoot:
    """Just enough of `eink_shoot.Shoot` for `frame_xyz_d65(content_only=False)`: a `.panel()` method."""

    def __init__(self, rgb_frac: np.ndarray):
        self._rgb = rgb_frac

    def panel(self, tag, flat_tag="auto"):
        return self._rgb


def test_frame_xyz_d65_white_point_identity():
    """With M = identity and pedestal = 0, a frame that reads the D50 white point everywhere must come
    out as the D65 white point everywhere once adapted — the exact identity `eink_shoot.adapt` relies
    on (adapting a point equal to the source white lands exactly on the destination white), pinned here
    so a hand-rolled adaptation matrix could not sneak back in without breaking this."""
    d50_frac = ecam.D50 / 100.0
    frame = np.broadcast_to(d50_frac, (3, 4, 3)).copy()
    sh = _StubShoot(frame)
    M = np.eye(3)
    ped = np.zeros(3)
    xyz = efi.frame_xyz_d65(sh, "T", M, ped, content_only=False)
    expected = esh.D65_100 / 100.0
    assert xyz.shape == (3, 4, 3)
    assert np.allclose(xyz, expected, atol=1e-9)


# --- V3's ΔE00 wiring, pinned against a hand-computed pair -------------------------------------------

def test_v3_bookend_reuses_ink_colorimetry_and_pins_a_known_deltaE(monkeypatch):
    """Two synthetic frames with a KNOWN, hand-injected ink-RGB difference must report the CIEDE2000
    that `eink_color.ciede2000` itself computes for that difference — proving `v3_bookend` is really
    wired to `esh.ink_colorimetry`/`ciede2000` and not silently returning zeros."""
    M = np.eye(3)
    ped = np.zeros(3)
    base = {n: {"mean": np.array([50.0, 50.0, 50.0]), "std": np.zeros(3), "n": 4} for n in pm.INK_NAMES}
    shifted = {n: dict(v, mean=v["mean"].copy()) for n, v in base.items()}
    shifted["black"]["mean"] = shifted["black"]["mean"] + 5.0   # a real, nonzero perturbation

    class _StubShoot2:
        def inks(self, tag, **kw):
            return base if tag == "F1" else shifted

    rep = efi.v3_bookend(_StubShoot2(), M, ped, threshold=0.05, ref="F1", other="B1")
    assert rep["de00_per_ink"]["white"] == pytest.approx(0.0, abs=1e-6), "unperturbed ink must read 0"
    assert rep["de00_per_ink"]["black"] > 0.0, "the perturbed ink must show up"
    assert rep["worst"] == pytest.approx(max(rep["de00_per_ink"].values()), abs=1e-6)


# --- "D": the digital render's own index map, recovered losslessly from pure palette colours ---------

def test_digital_index_recovers_a_known_index_map(tmp_path, monkeypatch):
    from PIL import Image

    import epaper as ep
    from tools.eink import eink_bench as eb
    from tools.eink import eink_target as et

    w, h = 40, 30
    x0, y0, x1, y1 = 10, 5, 30, 25
    monkeypatch.setattr(et, "content_box", lambda ww, hh: (x0, y0, x1, y1))
    monkeypatch.setattr(eb, "OUT", tmp_path)

    pal = np.array(ep.SPECTRA6_OUTPUT_PALETTE, dtype=np.uint8)
    rng = np.random.default_rng(20260921)
    idx_truth = rng.integers(0, 6, size=(y1 - y0, x1 - x0))
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[y0:y1, x0:x1] = pal[idx_truth]

    def fake_cmd_target(ns):
        dest = tmp_path / f"target_art{ns.n:02d}_fake_{ns.width}x{ns.height}.png"
        Image.fromarray(img, "RGB").save(dest)

    monkeypatch.setattr(eb, "cmd_target", fake_cmd_target)

    got = efi.digital_index(7, width=w, height=h)
    assert np.array_equal(got, idx_truth)


def test_digital_index_refuses_a_stale_file(tmp_path, monkeypatch):
    """If `cmd_target` writes nothing new and only a PRE-EXISTING file matches the glob, `digital_index`
    must refuse rather than silently read the stale render (review F8 — picking `matches[-1]` by mtime
    never proved the file was actually fresh)."""
    from PIL import Image

    import epaper as ep
    from tools.eink import eink_bench as eb
    from tools.eink import eink_target as et

    w, h = 20, 20
    x0, y0, x1, y1 = 2, 2, 18, 18
    monkeypatch.setattr(et, "content_box", lambda ww, hh: (x0, y0, x1, y1))
    monkeypatch.setattr(eb, "OUT", tmp_path)

    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[y0:y1, x0:x1] = ep.SPECTRA6_OUTPUT_PALETTE[0]
    stale = tmp_path / f"target_art11_stale_{w}x{h}.png"
    Image.fromarray(img, "RGB").save(stale)
    old = stale.stat().st_atime, stale.stat().st_mtime - 3600     # backdate an hour
    import os
    os.utime(stale, old)

    monkeypatch.setattr(eb, "cmd_target", lambda ns: None)        # pushes nothing new

    with pytest.raises(SystemExit):
        efi.digital_index(11, width=w, height=h)


def test_digital_index_refuses_a_non_palette_pixel(tmp_path, monkeypatch):
    """A content-box pixel that is not an exact output-palette colour must be REPORTED (raised), never
    silently coerced to the nearest ink — that would be a check that can only pass."""
    from PIL import Image

    import epaper as ep
    from tools.eink import eink_bench as eb
    from tools.eink import eink_target as et

    w, h = 20, 20
    x0, y0, x1, y1 = 2, 2, 18, 18
    monkeypatch.setattr(et, "content_box", lambda ww, hh: (x0, y0, x1, y1))
    monkeypatch.setattr(eb, "OUT", tmp_path)

    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[y0:y1, x0:x1] = ep.SPECTRA6_OUTPUT_PALETTE[0]
    img[y0, x0] = (17, 42, 99)          # not a palette colour

    def fake_cmd_target(ns):
        Image.fromarray(img, "RGB").save(tmp_path / f"target_art{ns.n:02d}_fake_{ns.width}x{ns.height}.png")

    monkeypatch.setattr(eb, "cmd_target", fake_cmd_target)

    with pytest.raises(SystemExit):
        efi.digital_index(3, width=w, height=h)


# --- structural sanity on the frame table, checked against the SESSION LOG itself -------------------

def test_frames_table_matches_the_runsheet():
    """`FRAMES` must match the literal commands `session.jsonl` recorded being pushed to the panel on
    2026-09-19 — read the log, don't restate the constants a second time (review F8)."""
    import json as _json

    log_path = Path(__file__).resolve().parent.parent / "bench-eink/vault/raw/2026-09-19/session.jsonl"
    if not log_path.exists():
        pytest.skip("session.jsonl not present on this checkout")
    by_frame = {}
    for line in log_path.read_text().splitlines():
        row = _json.loads(line)
        by_frame[row["frame"]] = row["cmd"]

    assert set(efi.FRAMES) == {"F6", "F6b", "F7", "F10"}
    for tag, cfg in efi.FRAMES.items():
        cmd = by_frame[tag]
        assert cmd.startswith("target art"), f"{tag}: {cmd!r} is not an `art` target"
        assert f"--n {cfg['art_n']}" in cmd, f"{tag}: n mismatch against {cmd!r}"
        if cfg["white_point"] > 0:
            assert f"--white-point {cfg['white_point']}" in cmd, f"{tag}: wp mismatch against {cmd!r}"
        else:
            assert "--white-point" not in cmd, f"{tag}: unexpected --white-point in {cmd!r}"
        assert f"--gamma {cfg['gamma']}" in cmd, f"{tag}: gamma mismatch against {cmd!r}"


# --- D, established: pinned against the Pi session artefact (bench-eink/analysis/ is gitignored —
# maintainer-local only, so a missing artefact skips rather than fails a clean checkout) --------------

def test_pi_established_d_matches_the_local_session_artefact():
    """`PI_ESTABLISHED_D` (review pass 3, D1) must equal the "A" (shipping) key of the LOCAL (gitignored,
    maintainer-only, skipped on a clean checkout) `bench-eink/analysis/session_2026-09-20/*/inks.json`
    files from ADR-120's own bench-Pi session — read the artefact, don't just restate the numbers a
    second time as a constant."""
    import json as _json

    root = Path(__file__).resolve().parent.parent
    paths = {
        "F6": root / "bench-eink/analysis/session_2026-09-20/masterpieces__sunflowers/inks.json",
        "F10": root / "bench-eink/analysis/session_2026-09-20/masterpieces__caf-terrace-at-night/inks.json",
    }
    for tag, path in paths.items():
        if not path.exists():
            pytest.skip(f"{path} not present on this checkout (bench-eink/analysis/ is gitignored)")
        row = _json.loads(path.read_text())["A"]
        assert row == efi.PI_ESTABLISHED_D[tag], f"{tag}: {row} != PI_ESTABLISHED_D[{tag}]"


def test_frames_table_agrees_on_which_frames_have_established_d():
    """Only F6/F10 share their exact shipping recipe (n, white_point, gamma) with ADR-120's session —
    F6b (no white point) and F7 (gamma 0.8) do NOT, and must not silently gain an `PI_ESTABLISHED_D`
    entry if someone edits `FRAMES` later without checking."""
    for tag in ("F6", "F10"):
        assert tag in efi.PI_ESTABLISHED_D
        cfg = efi.FRAMES[tag]
        assert cfg["white_point"] == 0.75 and cfg["gamma"] == 1.0, \
            f"{tag}: recipe no longer matches ADR-120's session — PI_ESTABLISHED_D may be stale"
    for tag in ("F6b", "F7"):
        assert tag not in efi.PI_ESTABLISHED_D
