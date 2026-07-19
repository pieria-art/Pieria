"""Unit tests for the e-paper render pipeline (Track B)."""

import io
from pathlib import Path

import pytest
from PIL import Image

from epaper import PALETTES, SPECTRA6_OUTPUT_PALETTE, _adaptive_gamma, render_for_epaper


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
