"""Unit tests for the e-paper render pipeline (Track B)."""

import io
from pathlib import Path

import pytest
from PIL import Image

from epaper import (
    PALETTES,
    SPECTRA6_OUTPUT_PALETTE,
    _adaptive_gamma,
    _chromatic_ink_hues,
    _hue_error,
    _hue_error_fraction,
    apply_chroma_curve,
    normalize_crop_box,
    pick_crop_for_aspect,
    render_for_epaper,
)


def _make_image(tmp_path: Path, name="src.jpg", mode="RGB", size=(240, 160)) -> Path:
    """Write a small multi-colour gradient so dithering has work to do."""
    img = Image.new(mode, size)
    px = img.load()
    w, h = size
    for y in range(h):
        for x in range(w):
            if mode == "RGBA":
                px[x, y] = (x % 256, y % 256, (x + y) % 256, 255)
            elif mode == "CMYK":
                px[x, y] = (x % 256, y % 256, (x + y) % 256, 0)
            elif mode == "L":
                px[x, y] = (x + y) % 256
            else:
                px[x, y] = (x % 256, y % 256, (x + y) % 256)
    path = tmp_path / name
    img.save(path, format="JPEG" if name.endswith((".jpg", ".jpeg")) else "PNG")
    return path


def _colors(data: bytes):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    return {c for _, c in im.getcolors(maxcolors=1 << 24)}


def test_focal_changes_cover_crop(tmp_path):
    # Tall source: top half red, bottom half blue. A wide cover-crop keeps a horizontal band,
    # so the focal Y selects which half survives.
    img = Image.new("RGB", (200, 400))
    px = img.load()
    for y in range(400):
        for x in range(200):
            px[x, y] = (220, 20, 20) if y < 200 else (20, 20, 220)
    src = tmp_path / "split.png"
    img.save(src)

    top = render_for_epaper(src, 400, 100, palette="spectra6", fit="cover", focal=(0.5, 0.0), fmt="png")
    bot = render_for_epaper(src, 400, 100, palette="spectra6", fit="cover", focal=(0.5, 1.0), fmt="png")
    assert top != bot  # focal moved which band was kept

    def dominant(data):
        im = Image.open(io.BytesIO(data)).convert("RGB")
        return max(im.getcolors(maxcolors=1 << 24))[1]
    rt, _, bt = dominant(top)
    rb, _, bb = dominant(bot)
    assert rt > bt   # top focal kept the red band
    assert bb > rb   # bottom focal kept the blue band


def test_focal_default_is_centered(tmp_path):
    src = _make_image(tmp_path)
    a = render_for_epaper(src, 300, 300, palette="gray4", fit="cover", fmt="png")
    b = render_for_epaper(src, 300, 300, palette="gray4", fit="cover", focal=(0.5, 0.5), fmt="png")
    assert a == b   # omitting focal == explicit center


def test_cover_exact_size_png(tmp_path):
    data = render_for_epaper(_make_image(tmp_path), 600, 400, palette="spectra6", fit="cover", fmt="png")
    im = Image.open(io.BytesIO(data))
    assert im.size == (600, 400)
    assert im.format == "PNG"


def test_output_colors_are_subset_of_palette(tmp_path):
    # spectra6 dithers toward the panel's real primaries but RE-ENCODES to pure primaries on output
    # (so any client — incl. an inky re-quantize — maps each colour unambiguously). See epaper.SPECTRA6_*.
    data = render_for_epaper(_make_image(tmp_path, "a.png"), 300, 200, palette="spectra6", fmt="png")
    assert _colors(data).issubset(set(SPECTRA6_OUTPUT_PALETTE))


def test_spectra6_adaptive_gamma_keys_on_wash(tmp_path):
    # Highlight pulldown is driven by flat low-chroma near-white ("wash") content, not brightness:
    # a woodblock-print-like pale neutral -> 1.5; a bright but CHROMATIC fill or a mid tone -> 1.4.
    washy = Image.new("RGB", (256, 256), (235, 236, 234))       # bright + near-neutral
    bright_colour = Image.new("RGB", (256, 256), (255, 255, 120))  # bright but high-chroma (yellow)
    midtone = Image.new("RGB", (256, 256), (120, 120, 120))     # not bright
    assert _adaptive_gamma(washy) == pytest.approx(1.5, abs=0.01)
    assert _adaptive_gamma(bright_colour) == pytest.approx(1.4, abs=0.01)
    assert _adaptive_gamma(midtone) == pytest.approx(1.4, abs=0.01)


def test_spectra6_enhance_false_skips_gamma(tmp_path):
    # enhance gates the adaptive gamma pulldown on the spectra6 path.
    src = _make_image(tmp_path, "e.png")
    with_gamma = render_for_epaper(src, 120, 120, palette="spectra6", fmt="png", enhance=True)
    no_gamma = render_for_epaper(src, 120, 120, palette="spectra6", fmt="png", enhance=False)
    assert with_gamma != no_gamma


def test_grayscale_palette_is_only_gray(tmp_path):
    data = render_for_epaper(_make_image(tmp_path, "g.png"), 200, 200, palette="gray4", fmt="png")
    for (r, g, b) in _colors(data):
        assert r == g == b


def test_contain_letterboxes_with_white(tmp_path):
    src = _make_image(tmp_path, "c.png", size=(400, 100))  # wide -> top/bottom padding
    data = render_for_epaper(src, 200, 200, palette="spectra6", fit="contain", fmt="png")
    im = Image.open(io.BytesIO(data))
    assert im.size == (200, 200)
    assert (255, 255, 255) in _colors(data)


def test_bmp_format(tmp_path):
    data = render_for_epaper(_make_image(tmp_path, "b.png"), 120, 120, palette="spectra6", fmt="bmp")
    im = Image.open(io.BytesIO(data))
    assert im.format == "BMP"
    assert im.size == (120, 120)


def test_acep7_subset_holds(tmp_path):
    data = render_for_epaper(_make_image(tmp_path, "o.png"), 100, 100, palette="acep7", fmt="png")
    assert _colors(data).issubset(set(PALETTES["acep7"]))


@pytest.mark.parametrize("mode,name", [("RGBA", "x.png"), ("CMYK", "x.jpg"), ("L", "x.png")])
def test_input_modes_are_normalized(tmp_path, mode, name):
    data = render_for_epaper(_make_image(tmp_path, name, mode=mode), 100, 100, palette="spectra6", fmt="png")
    assert Image.open(io.BytesIO(data)).size == (100, 100)


def test_invalid_palette_raises(tmp_path):
    with pytest.raises(ValueError):
        render_for_epaper(_make_image(tmp_path, "p.png"), 100, 100, palette="nope", fmt="png")


def test_invalid_format_raises(tmp_path):
    with pytest.raises(ValueError):
        render_for_epaper(_make_image(tmp_path, "f.png"), 100, 100, palette="spectra6", fmt="gif")


# --- per-aspect crop presets -------------------------------------------------
# Tier 2 framing: an authored box per screen shape, because a focal point can only SLIDE a
# fixed-size window, never CHOOSE one. Not to be confused with build_pack's Tier-1 crop_box
# (de-bordering "what IS the artwork"), which is baked into the master well before we render.


def _quadrant_src(tmp_path) -> Path:
    """400x400, four solid quadrants — a crop box selects an identifiable one."""
    img = Image.new("RGB", (400, 400))
    px = img.load()
    for y in range(400):
        for x in range(400):
            px[x, y] = ((220, 20, 20) if x < 200 else (20, 220, 20)) if y < 200 else \
                       ((20, 20, 220) if x < 200 else (230, 230, 20))
    src = tmp_path / "quad.png"
    img.save(src)
    return src


def test_crop_box_selects_region(tmp_path):
    src = _quadrant_src(tmp_path)
    tl = render_for_epaper(src, 100, 100, palette="spectra6", fmt="png", crop_box=(0.0, 0.0, 0.5, 0.5))
    br = render_for_epaper(src, 100, 100, palette="spectra6", fmt="png", crop_box=(0.5, 0.5, 1.0, 1.0))
    assert tl != br
    assert _colors(tl) == {(255, 0, 0)}      # top-left quadrant is solid red
    assert _colors(br) == {(255, 255, 0)}    # bottom-right is solid yellow


def test_no_crop_box_is_byte_identical(tmp_path):
    """The whole feature must be inert until crop data exists — no silent reframing on upgrade."""
    src = _quadrant_src(tmp_path)
    base = render_for_epaper(src, 120, 90, palette="spectra6", fmt="png", focal=(0.4, 0.3))
    assert render_for_epaper(src, 120, 90, palette="spectra6", fmt="png", focal=(0.4, 0.3),
                             crop_box=None) == base


def test_full_frame_crop_box_is_noop(tmp_path):
    """[0,0,1,1] means 'the whole frame already is the crop' — must not perturb the render."""
    src = _quadrant_src(tmp_path)
    base = render_for_epaper(src, 120, 90, palette="spectra6", fmt="png", focal=(0.4, 0.3))
    assert render_for_epaper(src, 120, 90, palette="spectra6", fmt="png", focal=(0.4, 0.3),
                             crop_box=(0.0, 0.0, 1.0, 1.0)) == base


@pytest.mark.parametrize("bad", [
    (0.5, 0.0, 0.2, 1.0),    # x1 <= x0
    (0.0, 0.9, 1.0, 0.4),    # y1 <= y0
    (-0.1, 0.0, 1.0, 1.0),   # out of range
    (0.0, 0.0, 1.5, 1.0),    # out of range
    (0.0, 0.0, 1.0),         # wrong arity
    ("a", "b", "c", "d"),    # non-numeric
])
def test_invalid_crop_box_falls_back_to_focal(tmp_path, bad):
    """A bad box must degrade to today's behaviour, never raise — device art keeps painting."""
    src = _quadrant_src(tmp_path)
    base = render_for_epaper(src, 120, 90, palette="spectra6", fmt="png", focal=(0.4, 0.3))
    assert render_for_epaper(src, 120, 90, palette="spectra6", fmt="png", focal=(0.4, 0.3),
                             crop_box=bad) == base


def test_normalize_crop_box_accepts_and_rejects():
    assert normalize_crop_box([0.1, 0.2, 0.9, 0.8]) == (0.1, 0.2, 0.9, 0.8)
    assert normalize_crop_box((0.0, 0.0, 1.0, 1.0)) is None      # near-full == no-op
    assert normalize_crop_box([0.0, 0.0, 0.999, 0.999]) is None  # within the 0.002 tolerance
    assert normalize_crop_box(None) is None
    assert normalize_crop_box([0.5, 0.5, 0.5, 0.9]) is None      # zero width


def test_pick_crop_for_aspect_picks_nearest_ratio():
    crops = {"16:9": [0, 0, 1, 0.5], "9:16": [0.2, 0, 0.6, 1],
             "4:3": [0, 0, 1, 0.75], "3:4": [0.1, 0, 0.8, 1]}
    assert pick_crop_for_aspect(crops, 3840, 2160) == (0.0, 0.0, 1.0, 0.5)     # exact 16:9
    assert pick_crop_for_aspect(crops, 1080, 1920) == (0.2, 0.0, 0.6, 1.0)     # exact 9:16
    assert pick_crop_for_aspect(crops, 1600, 1200) == (0.0, 0.0, 1.0, 0.75)    # exact 4:3
    # an odd panel still gets the closest preset rather than falling back to a focal cover
    assert pick_crop_for_aspect(crops, 1872, 1404) == (0.0, 0.0, 1.0, 0.75)


def test_pick_crop_for_aspect_degrades_quietly():
    assert pick_crop_for_aspect({}, 1600, 1200) is None
    assert pick_crop_for_aspect(None, 1600, 1200) is None
    assert pick_crop_for_aspect({"bogus": [0, 0, 1, 1]}, 1600, 1200) is None
    assert pick_crop_for_aspect({"4:3": "nonsense"}, 1600, 1200) is None
    assert pick_crop_for_aspect({"4:3": [0, 0, 1, 0.75]}, 1600, 0) is None     # no divide-by-zero


# --- Hue-conditioned chroma curve (ADR-088 correction) ------------------------------------------

def _solid(rgb, size=(64, 64)):
    return Image.new("RGB", size, rgb)


def _mean_chroma(img):
    px = list(img.convert("RGB").getdata())
    return sum(max(p) - min(p) for p in px) / len(px)


def test_chromatic_ink_hues_excludes_black_and_white():
    """The floor is keyed on hue, and black/white carry none — including them would put a spurious
    'well-served' hue peak wherever their nominal hue happens to land."""
    hues = _chromatic_ink_hues()
    assert len(hues) == 4, f"expected the 4 chromatic inks, got {hues}"


def test_hue_error_peaks_between_inks_and_vanishes_on_them():
    for ink_hue in _chromatic_ink_hues():
        assert _hue_error(ink_hue) == 0.0
    # midway between the two closest inks nothing serves the hue well
    a, b = sorted(_chromatic_ink_hues())[:2]
    assert _hue_error((a + b) / 2.0) > 0.0


def test_chroma_curve_crushes_an_unservable_hue_and_spares_a_servable_one():
    """The whole point: same lever, opposite outcomes, decided by hue alone.

    Guards the ADR-088 correction — June's skin (a hue no ink serves) must lose its faint colour
    while Sunflowers' pale wall (essentially the yellow ink's own hue) must keep it.
    """
    inks = _chromatic_ink_hues()
    served = int(min(inks))                                   # sits exactly on an ink
    # the WORST-served hue, found rather than assumed: a fixed offset can silently land on another
    # ink (yellow+64 is green, which is how the first version of this test fooled itself).
    unserved = max(range(256), key=_hue_error)
    assert _hue_error(unserved) > _hue_error(served)

    on_ink = Image.new("HSV", (64, 64), (served, 90, 220)).convert("RGB")
    off_ink = Image.new("HSV", (64, 64), (unserved, 90, 220)).convert("RGB")

    kept = apply_chroma_curve(on_ink, chroma_gamma=2.0, floor_max=0.7, hue_e0=20.0)
    crushed = apply_chroma_curve(off_ink, chroma_gamma=2.0, floor_max=0.7, hue_e0=20.0)

    kept_ratio = _mean_chroma(kept) / max(_mean_chroma(on_ink), 1e-6)
    crushed_ratio = _mean_chroma(crushed) / max(_mean_chroma(off_ink), 1e-6)
    assert kept_ratio > crushed_ratio, (
        f"a hue an ink can serve must keep more chroma than one none can "
        f"(kept {kept_ratio:.3f} vs crushed {crushed_ratio:.3f})")


def test_chroma_curve_is_a_noop_when_disabled():
    src = _solid((200, 120, 60))
    assert apply_chroma_curve(src, chroma_gamma=1.0, floor_max=0.0, hue_e0=20.0) is src


def test_chroma_curve_preserves_size_and_mode():
    src = _solid((200, 120, 60), size=(80, 40))
    out = apply_chroma_curve(src, chroma_gamma=2.0, floor_max=0.7, hue_e0=20.0)
    assert out.size == (80, 40) and out.mode == "RGB"


def test_chroma_curve_never_raises_saturation():
    """s' = max(s**k, s*floor) with k>=1 and floor<=1 can only attenuate — a curve that BOOSTS
    chroma would push more colour at a palette that is already over-saturating."""
    src = Image.new("HSV", (64, 64), (30, 200, 200)).convert("RGB")
    out = apply_chroma_curve(src, chroma_gamma=2.0, floor_max=1.0, hue_e0=20.0)
    assert _mean_chroma(out) <= _mean_chroma(src) + 1.0


def test_gap_normalised_floor_has_no_dead_zones():
    """The absolute-cutoff floor annihilated 38% of the hue circle, and only in the WIDE ink gaps.

    Guards the 2026-08-28 panel finding: the chromatic inks are unevenly spaced (yellow 36, green
    100, blue 172, red 253), so a fixed cutoff spares the narrow warm arc and wipes out cool faint
    colour — a woodblock's pale blue vanished. Gap normalisation must reach zero only AT a midpoint.
    """
    dead_abs = [h for h in range(256) if 0.7 * max(0.0, 1 - _hue_error(h) / 20.0) == 0.0]
    dead_gap = [h for h in range(256) if 0.7 * (1 - _hue_error_fraction(h)) <= 0.0]
    assert len(dead_abs) > 80, "expected the absolute cutoff to have wide dead zones"
    assert len(dead_gap) <= 6, f"gap normalisation should kill only midpoints, got {dead_gap}"


def test_gap_normalised_floor_peaks_on_every_ink():
    for ink in _chromatic_ink_hues():
        assert _hue_error_fraction(ink) == 0.0, f"floor must be full on ink hue {ink}"


def test_floor_min_keeps_colour_everywhere():
    """floor_min is the guarantee that no hue is ever stripped completely."""
    worst = max(range(256), key=_hue_error_fraction)
    # FAINT colour specifically: floor_min only bites where s*floor_min exceeds s**k, i.e. below
    # s = floor_min**(1/(k-1)). At k=2, floor_min=0.35 that is s < 0.35 -- saturated content is
    # governed by the curve, not the floor, which is exactly the intended division of labour.
    src = Image.new("HSV", (32, 32), (worst, 40, 200)).convert("RGB")
    stripped = apply_chroma_curve(src, 2.0, 0.7, 20.0, gap_normalised=True, floor_min=0.0)
    kept = apply_chroma_curve(src, 2.0, 0.7, 20.0, gap_normalised=True, floor_min=0.35)
    assert _mean_chroma(kept) > _mean_chroma(stripped)
