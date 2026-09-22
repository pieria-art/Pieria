"""
tests/test_eink_candidate.py — ADR-084 candidate session tool (tools/eink/eink_candidate.py).

Small synthetic 64x48 images throughout (a real library master is unnecessary and slow); the DB-backed
`eink_bench._db_crop_and_focal` / `_authored_box` are monkeypatched so no `data/artwork.db` is needed.
"""
import builtins
import io
import json

import numpy as np
import pytest
from PIL import Image

import epaper as ep
from tools.eink import eink_candidate as cand
from tools.eink import eink_color as ec
from tools.eink import eink_panel_model as pm
from tools.eink import eink_push


def _make_library_image(tmp_path, name="testcoll__test-title__deadbeef.jpg", size=(64, 48), seed=0):
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8)
    path = tmp_path / name
    Image.fromarray(arr, mode="RGB").save(path, "JPEG", quality=95)
    return path


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """No `data/artwork.db` in a test run — centered, uncropped framing by default, unless a test
    overrides it."""
    monkeypatch.setattr(cand.eb, "_db_crop_and_focal", lambda filename, w, h: (None, (0.5, 0.5)))
    monkeypatch.setattr(cand.eb, "_authored_box", lambda n: None)


# --- Variant A: byte-identical to what ships ----------------------------------------------------

def test_variant_a_is_byte_identical_to_render_for_epaper(tmp_path):
    img = _make_library_image(tmp_path)
    crop, focal, authored = cand.frame_work(img, None, 64, 48)
    assert authored is False

    idx, arr = cand.variant_a_index(img, 64, 48, focal, crop)
    png = ep.render_for_epaper(img, 64, 48, palette="spectra6", fit="cover", focal=focal,
                                fmt="png", enhance=True, crop_box=tuple(crop) if crop else None)
    want = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.uint8)
    assert np.array_equal(arr, want), "variant A must decode to exactly what render_for_epaper produced"
    assert np.array_equal(np.asarray(cand.panel_rgb(idx)), want), \
        "the panel-ready re-encode from the index map must round-trip byte-identical"


# --- Variant B: measured-palette barycentric -----------------------------------------------------

def test_variant_b_index_map_is_in_range_and_fractions_sum_to_100(tmp_path):
    img = _make_library_image(tmp_path)
    fitted = ep._fit_rgb(img, 64, 48, "cover", (0.5, 0.5), None)
    idx = cand.variant_b_index(fitted, seed=7, salt="testcoll__test-title")

    assert idx.dtype == np.uint8
    assert set(np.unique(idx).tolist()) <= {0, 1, 2, 3, 4, 5}

    fracs = cand.ink_fractions(idx)
    assert set(fracs) == set(pm.INK_NAMES)
    assert sum(fracs.values()) == pytest.approx(100.0, abs=0.6)


def test_previews_use_exactly_the_measured_ink_colours(tmp_path):
    img = _make_library_image(tmp_path)
    allowed = {tuple(row) for row in pm.ink_srgb8().tolist()}

    fitted = ep._fit_rgb(img, 64, 48, "cover", (0.5, 0.5), None)
    idx_b = cand.variant_b_index(fitted, seed=3, salt="x")
    prev_b = np.asarray(cand.preview_rgb(idx_b)).reshape(-1, 3)
    assert {tuple(px) for px in np.unique(prev_b, axis=0).tolist()} <= allowed

    crop, focal, _ = cand.frame_work(img, None, 64, 48)
    idx_a, _ = cand.variant_a_index(img, 64, 48, focal, crop)
    prev_a = np.asarray(cand.preview_rgb(idx_a)).reshape(-1, 3)
    assert {tuple(px) for px in np.unique(prev_a, axis=0).tolist()} <= allowed


# --- Session: determinism, blinding, pushlist -----------------------------------------------------

def test_blinding_is_deterministic_and_each_work_has_one_a_and_one_b(tmp_path):
    img1 = _make_library_image(tmp_path, name="collone__title-one__aaaaaaaa.jpg", seed=1)
    img2 = _make_library_image(tmp_path, name="colltwo__title-two__bbbbbbbb.jpg", seed=2)
    out1, out2 = tmp_path / "out1", tmp_path / "out2"

    cand.run_session([str(img1), str(img2)], out1, 64, 48, seed=42)
    cand.run_session([str(img1), str(img2)], out2, 64, 48, seed=42)

    b1 = json.loads((out1 / "blinding.json").read_text())
    b2 = json.loads((out2 / "blinding.json").read_text())
    assert b1 == b2, "same seed + same work list must reproduce identical blinding"
    assert b1["seed"] == 42
    assert len(b1["works"]) == 2
    for w in b1["works"]:
        assert set(w["labels"]) == {"L", "R"}
        assert set(w["labels"].values()) == {"A", "B"}, "each work must carry exactly one A and one B"

    for p in sorted(out1.rglob("*")):
        if p.is_file():
            rel = p.relative_to(out1)
            assert (out2 / rel).read_bytes() == p.read_bytes(), f"{rel} differs between identical runs"


def test_pushlist_has_two_lines_per_work(tmp_path):
    img1 = _make_library_image(tmp_path, name="collone__title-one__aaaaaaaa.jpg", seed=1)
    img2 = _make_library_image(tmp_path, name="colltwo__title-two__bbbbbbbb.jpg", seed=2)
    out = tmp_path / "out"
    results = cand.run_session([str(img1), str(img2)], out, 64, 48, seed=5)

    lines = (out / "pushlist.txt").read_text().strip().splitlines()
    assert len(lines) == 2 * len(results)

    by_slug: dict = {}
    for ln in lines:
        slug, label, relpath = ln.split(" ", 2)
        by_slug.setdefault(slug, []).append((label, relpath))
    for r in results:
        entries = by_slug[r["slug"]]
        assert len(entries) == 2
        assert {e[0] for e in entries} == {"L", "R"}
        for _label, relpath in entries:
            assert (out / relpath).exists()


def test_authored_box_overrides_only_for_corpus_numbers(tmp_path, monkeypatch):
    img = _make_library_image(tmp_path)
    monkeypatch.setattr(cand.eb, "_db_crop_and_focal",
                         lambda filename, w, h: ((0.0, 0.0, 1.0, 1.0), (0.5, 0.5)))
    monkeypatch.setattr(cand.eb, "_authored_box", lambda n: (0.1, 0.1, 0.9, 0.9) if n == 1 else None)

    crop, focal, authored = cand.frame_work(img, 1, 64, 48)
    assert authored is True
    assert crop == (0.1, 0.1, 0.9, 0.9)

    crop2, focal2, authored2 = cand.frame_work(img, None, 64, 48)
    assert authored2 is False
    assert crop2 == (0.0, 0.0, 1.0, 1.0), "no corpus n -> the DB crop stands, never an authored override"


# --- The ADR-108 property, correctly scoped for today's palette + solver -------------------------

def test_variant_b_neutral_field_has_no_colour_cast():
    """ADR-108 (2026-08-30) measured a flat neutral field decomposing from black+white ONLY, with the
    barycentric decomposer's exact weights carrying "no error at all" — contrasted with
    Floyd-Steinberg's measured colour cast beside an out-of-gamut region.

    ⚠️ "black+white only" does NOT survive into today's palette + solver — checked directly: across
    the full 0..255 flat-grey sweep, under the measured inks (the default) no source level decomposes
    to {black, white} alone (the measured black ink itself carries chroma, a* +9.3, ADR-116, so the
    default black-point compensation — lifting source black onto the achievable floor — needs a
    mixture even at the endpoints); under the vendor swatch it's true only exactly AT 0 and 255, not
    for any interior grey. `decompose()`'s own tie-break (minimise the LARGEST weight) prefers a
    smoother multi-ink blend over the sparser two-ink edge split whenever that lowers the max weight,
    which is most of the time — a deliberate choice, not a bug (see its docstring).

    What DOES survive, and is asserted here: weights are exact by construction
    (sum(w_i * ink_i) == the target, for whichever tetrahedron `decompose` picks), so a flat neutral
    field's REALISED mean colour carries no cast, however many inks the weight spreads across.
    """
    idx = cand.variant_b_index(Image.new("RGB", (64, 48), (128, 128, 128)), seed=11, salt="neutral")
    inks_lin = ec.xyz_to_linear_rgb(pm.ink_xyz())
    realised = inks_lin[idx].reshape(-1, 3).mean(axis=0)
    lab = ec.xyz_to_lab(ec.linear_rgb_to_xyz(realised), pm.media_white())
    assert abs(lab[1]) < 1.0 and abs(lab[2]) < 1.0, f"colour cast on a neutral field: a*={lab[1]:.3f} b*={lab[2]:.3f}"


# --- eink_push: Pi-only dependency handling --------------------------------------------------------

def test_eink_push_exits_2_without_inky(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name.startswith("inky"):
            raise ImportError("no module named inky")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    png = tmp_path / "x.png"
    Image.new("RGB", (4, 4)).save(png)

    with pytest.raises(SystemExit) as exc:
        eink_push.push(png)
    assert exc.value.code == 2
