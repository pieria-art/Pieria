"""The photograph -> measurement pipeline, validated against synthetic photographs.

Built and proven before any camera existed: a known perspective warp plus a known camera colour
distortion is applied to a known target, and the pipeline must recover the truth. That is a stronger
guarantee than pointing it at a panel and agreeing with the result, because here the answer is known.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

np = pytest.importorskip("numpy", reason="measurement tooling is maintainer-only, numpy not shipped")

from PIL import Image  # noqa: E402

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


def test_read_panel_accepts_already_rectified_float_array():
    """The eink_raw hook: a caller with already-rectified scene-linear float64 data (1.0 == sensor
    saturation, `tools.eink_raw.RawFrame.rgb`'s own convention) must be able to skip rectify()
    entirely and still get sane normalise/measure output — this is the seam a raw-camera capture
    path plugs into, and it must work without ever having gone through an 8-bit PIL Image.
    """
    photo = em._synthesise_photo(_target(), warp=0.0, gain=(1, 1, 1), off=(0, 0, 0), noise=0.0, seed=2)
    pad = int(max(W, H) * 0.12)
    roi = (pad, pad, photo.width - pad, photo.height - pad)
    rect_img = em.rectify(photo, W, H, roi)
    rect_float = np.asarray(rect_img).astype(np.float64) / 255.0     # eink_raw's own convention
    r = em.read_panel(rect_float, W, H)
    got = em.measured_primaries(r["corrected"], W, H)
    truth = {n: tuple(c) for n, c in zip(et.INK_NAMES, ep.SPECTRA6_OUTPUT_PALETTE)}
    for name in et.INK_NAMES:
        worst = max(abs(a - b) for a, b in zip(got[name], truth[name]))
        assert worst <= 5, f"{name}: measured {got[name]} vs {truth[name]}"


def test_read_panel_float_path_rejects_reference_and_roi():
    """Both `reference=` and `roi=` depend on rectify()/align_to_reference, which are PIL/8-bit-only
    by design (see read_panel's docstring) — an already-rectified array must fail loudly rather than
    silently ignoring either, since a silent ignore here would look like a working alignment."""
    rect_float = np.zeros((H, W, 3), dtype=np.float64)
    sentinel = object()
    with pytest.raises(NotImplementedError):
        em.read_panel(rect_float, W, H, reference=sentinel)
    with pytest.raises(NotImplementedError):
        em.read_panel(rect_float, W, H, roi=(0, 0, W, H))


def test_float_path_preserves_precision_the_8bit_path_collapses():
    """The whole point of closing GAP 2: a uint8 buffer cannot represent a difference smaller than
    one level, so two genuinely distinct patches collapse to the identical number once
    `apply_correction` rounds them. Build exactly that pair — two halves of a content quadrant
    0.2/255 apart in `tools.eink_raw.RawFrame.rgb`'s own convention — and show the float path (this
    change) keeps them apart while the pre-existing 8-bit path (still exercised here, unchanged)
    genuinely does collapse them. A test that only shows "it runs" would not prove retained
    precision; this one demonstrates the specific number that would otherwise be lost.
    """
    photo = em._synthesise_photo(_target(), warp=0.0, gain=(1, 1, 1), off=(0, 0, 0), noise=0.0, seed=5)
    pad = int(max(W, H) * 0.12)
    roi = (pad, pad, photo.width - pad, photo.height - pad)
    rect_img = em.rectify(photo, W, H, roi)
    base = np.asarray(rect_img).astype(np.float64) / 255.0     # eink_raw's own convention

    x0, y0, x1, y1 = et.content_box(W, H)
    cw, ch = x1 - x0, y1 - y0
    idx = et.INK_NAMES.index("green")
    cx, cy = idx % 3, idx // 3
    qx0, qy0 = x0 + cx * cw // 3, y0 + cy * ch // 2
    qx1, qy1 = x0 + (cx + 1) * cw // 3, y0 + (cy + 1) * ch // 2
    mid = (qx0 + qx1) // 2
    a = base.copy()
    a[qy0:qy1, mid:qx1, :] += 0.2 / 255.0     # sub-single-8-bit-level split, right half only

    r = em.read_panel(a, W, H)
    corrected = r["corrected"]
    assert isinstance(corrected, np.ndarray) and corrected.dtype == np.float64, (
        "read_panel's float entry point must hand back float, not a re-quantised PIL Image")

    inset = 3
    left_f = float(corrected[qy0 + inset:qy1 - inset, qx0 + inset:mid - inset, :].mean())
    right_f = float(corrected[qy0 + inset:qy1 - inset, mid + inset:qx1 - inset, :].mean())
    assert abs(right_f - left_f) > 0.05, (
        f"float path lost the sub-LSB split: left={left_f} right={right_f}")

    # The pre-existing 8-bit path, run over the SAME corrected values, is expected to collapse them
    # — that is the bug this change closes, demonstrated rather than asserted away.
    rect_arr = a * 255.0
    gain, off = np.asarray(r["gain"]), np.asarray(r["offset"])
    quantised = np.asarray(em.apply_correction(rect_arr, gain, off)).astype(np.float64)
    left_u = float(quantised[qy0 + inset:qy1 - inset, qx0 + inset:mid - inset, :].mean())
    right_u = float(quantised[qy0 + inset:qy1 - inset, mid + inset:qx1 - inset, :].mean())
    assert left_u == right_u, (
        "expected the 8-bit path to collapse this sub-LSB difference; if it no longer does, this "
        "test's construction needs a larger split")


def test_rectify_float_recovers_known_warp():
    """Synthetic known-perspective-warp round-trip through the float rectifier: same corner
    detection as `rectify()` (fed an 8-bit proxy of the float data), but the resample happens in
    float. Must recover the original target to a tolerance that accounts for the two paths' only
    real difference — BILINEAR here vs BICUBIC in `rectify()` — not for a broken homography.
    """
    target = _target()
    photo = em._synthesise_photo(target, warp=0.02, gain=(1, 1, 1), off=(0, 0, 0), noise=0.0, seed=9)
    pad = int(max(W, H) * 0.12)
    roi = (pad, pad, photo.width - pad, photo.height - pad)
    photo_float = np.asarray(photo.convert("RGB")).astype(np.float64) / 255.0   # eink_raw's own axis

    rectified = em.rectify_float(photo_float, W, H, roi)
    assert rectified.shape == (H, W, 3)
    assert rectified.dtype == np.float64

    truth = np.asarray(target.convert("RGB")).astype(np.float64) / 255.0
    x0, y0, x1, y1 = et.content_box(W, H)
    err = np.abs(rectified[y0:y1, x0:x1] - truth[y0:y1, x0:x1])
    assert float(err.mean()) < 0.02, f"mean abs error too high: {float(err.mean())}"


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


def _shaded(img, shade):
    """Multiply a composed target/photo by a spatial gradient — stands in for a REAL illumination
    gradient across the panel, so dividing it back out (the flat field's whole job) is something the
    pipeline actually has to do, rather than a no-op that would hide an ordering bug."""
    arr = np.asarray(img.convert("RGB")).astype(float) * shade
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def test_veiling_glare_recovered_only_with_correct_trap_subtraction():
    """TASK: subtract trap pedestal -> divide flat field -> affine. Flare is additive, the flat field
    is a multiplicative illumination map, and those two do not commute. A real illumination gradient
    is included (a uniform flat field would make the division a near no-op and prove nothing about
    ordering), so recovery genuinely depends on both DOING the subtraction and doing it in the right
    place in the pipeline.

    Two failure modes are checked, both against the SAME correctly-cleaned flat field so neither can
    be blamed on `build_flat_field`: omitting the subtraction entirely, and doing the arithmetic with
    the two operations swapped (divide, then subtract — what an accidental reordering of `read_panel`
    would produce). Both must fail badly, not just "worse" — a fuzzy margin would make this test as
    likely to rot silently as the bug it guards against.
    """
    shade = np.broadcast_to(np.linspace(0.75, 1.25, W).reshape(1, W, 1), (H, W, 3))
    content_target = _shaded(_target(), shade)
    flat_target = _shaded(et.compose(et.target_flat(W, H), W, H), shade)

    flare = (24.0, 30.0, 20.0)   # what this rig's light trap reads this session — additive, uniform
    content_photo = em._synthesise_photo(content_target, warp=0.0, gain=(1, 1, 1), off=(0, 0, 0),
                                         noise=0.0, seed=12, flare=flare)
    flat_photo = em._synthesise_photo(flat_target, warp=0.0, gain=(1, 1, 1), off=(0, 0, 0),
                                      noise=0.0, seed=13, flare=flare)

    truth = {n: tuple(c) for n, c in zip(et.INK_NAMES, ep.SPECTRA6_OUTPUT_PALETTE)}

    def worst_error(corrected):
        got = em.measured_primaries(corrected, W, H)
        return max(max(abs(a - b) for a, b in zip(got[n], truth[n])) for n in truth)

    flat_clean = em.build_flat_field(flat_photo, W, H, trap=flare)

    # CORRECT: subtract, then divide — read_panel's actual order.
    r_correct = em.read_panel(content_photo, W, H, flat=flat_clean, trap=flare)
    assert worst_error(r_correct["corrected"]) <= 15, "correct order failed to recover the truth"

    # OMITTED: trap=None. The flare rides straight into the black/white patch fit.
    r_omitted = em.read_panel(content_photo, W, H, flat=flat_clean)
    assert worst_error(r_omitted["corrected"]) > 100, (
        "omitting trap subtraction should NOT recover the truth — this test proves nothing if it does")

    # MIS-ORDERED: divide by the flat field FIRST, subtract the trap reading AFTER — built by hand
    # from the same pieces `read_panel` composes internally (`rectify`, `solve_correction`,
    # `apply_correction`), with the operations swapped, so this is a direct probe of ORDER rather
    # than of any particular call's arguments.
    rect = np.asarray(em.rectify(content_photo, W, H)).astype(float)
    wrong = (rect / flat_clean * flat_clean.mean()) - np.asarray(flare)
    wrong_img = Image.fromarray(np.clip(wrong, 0, 255).astype(np.uint8), "RGB")
    gain, off, _ = em.solve_correction(wrong_img, W, H)
    wrong_corrected = em.apply_correction(wrong_img, gain, off)
    assert worst_error(wrong_corrected) > 100, (
        "dividing before subtracting should NOT recover the truth — this test proves nothing if it does")


def test_build_flat_field_trap_is_optional_and_a_no_op_when_absent():
    """`trap=None` (the default) must reproduce the pre-existing field byte-for-byte — the physical
    light trap does not exist yet, so every current caller must be unaffected."""
    flat_target = et.compose(et.target_flat(W, H), W, H)
    photo = em._synthesise_photo(flat_target, warp=0.0, gain=(1, 1, 1), off=(0, 0, 0), noise=0.0,
                                 seed=14)
    a = em.build_flat_field(photo, W, H)
    b = em.build_flat_field(photo, W, H, trap=None)
    assert np.array_equal(a, b)


def test_build_flat_field_subtracts_the_trap_pedestal():
    """🔴 THE SUBTLE PART (module docstring): the flat-field reference is itself a photograph, so it
    carries the same veiling glare as any other shot on the rig. Given a trap reading, the returned
    field must be lower by approximately that amount — otherwise a caller cleaning the main photo but
    not the flat (the mistake the docstring calls invisible after the fact) would still divide by a
    flare-contaminated map.
    """
    flat_target = et.compose(et.target_flat(W, H), W, H)
    flare = (18.0, 22.0, 26.0)
    photo = em._synthesise_photo(flat_target, warp=0.0, gain=(1, 1, 1), off=(0, 0, 0), noise=0.0,
                                 seed=15, flare=flare)
    dirty = em.build_flat_field(photo, W, H)
    clean = em.build_flat_field(photo, W, H, trap=flare)
    x0, y0, x1, y1 = et.content_box(W, H)
    delta = (dirty - clean)[y0:y1, x0:x1].mean(axis=(0, 1))
    for got, want in zip(delta, flare):
        assert abs(got - want) < 2.0, f"expected the field to drop by ~{flare}, got {delta}"


def test_raw_cli_cleans_the_flat_field_with_its_own_trap_reading(monkeypatch, tmp_path):
    """The wiring guard for the subtlest bug in this pipeline.

    `build_flat_field(..., trap=...)` is unit-tested above, but nothing checked that the RAW CLI
    actually PASSES it. That matters more than a normal wiring gap, because the failure is quiet:
    `solve_correction` re-fits the black/white anchors afterwards and partially absorbs an uncleaned
    flat field, so the end-to-end primaries barely move and no downstream number screams. A dropped
    `trap=` here would therefore survive every other test in this file.

    Also pins the second half of the same rule: the flat field's pedestal must be read from the FLAT
    PHOTO's own exposure, not reused from the content frame — two exposures, two flare readings.
    """
    seen = {}

    def spy_build(flat_photo, w, h, roi=None, smooth=40, trap=None):
        # Records the argument and returns a stand-in field. build_flat_field's OWN behaviour is
        # covered by test_build_flat_field_subtracts_the_trap_pedestal; what is under test here is
        # only whether the CLI hands it a trap reading at all, and the right one.
        seen["trap"] = None if trap is None else np.asarray(trap, dtype=float).copy()
        return np.ones((h, w, 3), dtype=np.float64)

    class _Frame:
        rgb = np.full((H, W, 3), 0.5, dtype=np.float64)
        black, dark_current, clipped_fraction = 512.0, 0.0, 0.0

    fake_raw = types.SimpleNamespace(decode=lambda p, dark_frame=None: _Frame())
    # BOTH of these are needed, and patching only sys.modules passes in isolation while failing in
    # the full suite: `from tools import eink_raw` reads the ATTRIBUTE off the already-imported
    # `tools` package, and only falls back to sys.modules when the submodule has not been imported
    # yet. tests/test_eink_raw.py imports it, so in a full run the attribute wins.
    import tools as _tools_pkg
    monkeypatch.setitem(sys.modules, "tools.eink_raw", fake_raw)
    monkeypatch.setattr(_tools_pkg, "eink_raw", fake_raw, raising=False)
    monkeypatch.setattr(em, "build_flat_field", spy_build)
    monkeypatch.setattr(em, "rectify_float", lambda a, w, h, roi=None: np.asarray(a, dtype=np.float64))
    monkeypatch.setattr(em, "read_panel", lambda *a, **k: {"corrected": None})
    monkeypatch.setattr(em, "_report_read", lambda *a, **k: None)

    # A flat photo whose trap aperture is BRIGHTER than the content frame's, so reusing the content
    # frame's reading instead of measuring this one would give a detectably different number.
    flat_png = tmp_path / "flat.png"
    flat_arr = np.full((H, W, 3), 200, dtype=np.uint8)
    flat_arr[0:20, 0:20] = 40
    Image.fromarray(flat_arr, "RGB").save(flat_png)

    args = types.SimpleNamespace(width=W, height=H, target="", roi="", trap="0,0,20,20",
                                 dark="", flat=str(flat_png), out=str(tmp_path / "o.png"),
                                 primaries=False)
    em._cmd_read_raw(args, Path("shot.ARW"))

    assert "trap" in seen, "build_flat_field was never called — the CLI wiring changed"
    assert seen["trap"] is not None, (
        "the raw CLI must pass trap= to build_flat_field: the flat reference was itself photographed "
        "through the same veiling glare, and leaving it in makes the correction self-inconsistent")
    assert np.allclose(seen["trap"], 40.0), (
        f"the flat field's pedestal must be measured from the FLAT photo's own aperture (40), not "
        f"reused from the content frame (127.5); got {seen['trap']}")
