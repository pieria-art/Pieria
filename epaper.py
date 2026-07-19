"""
E-paper rendering for the stateless per-display image endpoint (Track B).

Crops/fits a source image to an exact panel size and quantizes it to a fixed
device palette with Floyd–Steinberg dithering, so a "dumb" frame (DIY ESP32 +
Waveshare, Inky Impression, a TRMNL in BYOS mode, etc.) can fetch a ready-to-blit
image over HTTP without running the JS Canvas app.

No dithering existed in the app before this; see ROADMAP.md (Track B).
"""

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance, ImageOps

# --- Palettes -----------------------------------------------------------------
# Nominal sRGB anchors per device family. Per-panel colour tuning is deferred
# (the same task for every palette and best done against real hardware).
PALETTES = {
    # E Ink Spectra 6 (E6): black, white, red, yellow, blue, green.
    "spectra6": [
        (0, 0, 0), (255, 255, 255), (191, 0, 0),
        (255, 243, 56), (0, 0, 178), (0, 156, 72),
    ],
    # E Ink ACeP / Gallery 7-colour: Spectra 6 set + orange.
    "acep7": [
        (0, 0, 0), (255, 255, 255), (191, 0, 0), (255, 243, 56),
        (0, 0, 178), (0, 156, 72), (228, 120, 0),
    ],
    # 2-bit greyscale (4 levels) — common small mono panels.
    "gray4": [(0, 0, 0), (85, 85, 85), (170, 170, 170), (255, 255, 255)],
    # 4-bit greyscale (16 levels).
    "gray16": [(i * 17, i * 17, i * 17) for i in range(16)],
}

# Colour palettes get a saturation pre-boost; greyscale ones don't.
_COLOR_PALETTES = {"spectra6", "acep7"}

# --- E Ink Spectra 6 (EL133UF1) per-panel calibration (bench-tuned 2026-07-19) ------------------------
# The nominal spectra6 anchors above are near-pure and MORE saturated than the panel can physically
# show, so Floyd-Steinberg over-diffused (heavy grain) and dithered deep reds toward orange. On real
# glass we instead dither toward the panel's MEASURED primaries (Pimoroni inky's EL133UF1
# SATURATED_PALETTE) — which matches inky-native quality but stays UNIVERSAL: the server produces the
# dithered frame and any dumb client (Waveshare/ESP32/TRMNL) just blits it, no inky lib required.
# Order matches PALETTES["spectra6"]: black, white, red, yellow, blue, green.
SPECTRA6_DITHER_PALETTE = [
    (0, 0, 0), (161, 164, 165), (156, 72, 75),
    (208, 190, 71), (61, 59, 94), (58, 91, 70),
]
# The dithered frame is RE-ENCODED to these PURE primaries (same pixel indices) before output, so every
# client maps each colour unambiguously — including an inky client's set_image(), whose internal
# re-quantize would otherwise snap our muted blue/green (61,59,94 / 58,91,70) to BLACK.
SPECTRA6_OUTPUT_PALETTE = [
    (0, 0, 0), (255, 255, 255), (255, 0, 0),
    (255, 255, 0), (0, 0, 255), (0, 255, 0),
]

# Requested extension -> (PIL format, media type).
VALID_FORMATS = {
    "png": ("PNG", "image/png"),
    "bmp": ("BMP", "image/bmp"),
}
VALID_FITS = ("cover", "contain")

_PALETTE_IMAGE_CACHE: dict = {}


def _fit_rgb(image_path: Path, w: int, h: int, fit: str = "cover",
             focal: tuple = (0.5, 0.5)) -> Image.Image:
    """Open a source image, honour EXIF orientation, normalise ANY input mode
    (JPEG/PNG/WebP, incl. CMYK / RGBA / palette / greyscale) to RGB, and fit it to
    exactly w x h — cover-crop (anchored on the normalized focal point, default
    centered) or contain (letterbox onto white).

    Shared by the e-ink renderer and the full-colour (Frame TV) renderer so the
    orient/crop behaviour can't drift between outputs.
    """
    if fit not in VALID_FITS:
        fit = "cover"
    with Image.open(image_path) as src:
        img = ImageOps.exif_transpose(src)
        # C5: composite any transparency onto white "paper" BEFORE flattening. A plain convert("RGB")
        # fills transparent regions with black, which renders wrong on a paper-white e-ink/Frame panel.
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            img = img.convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img).convert("RGB")
        else:
            img = img.convert("RGB")
        if fit == "cover":
            return ImageOps.fit(
                img, (w, h), method=Image.Resampling.LANCZOS, centering=focal
            )
        # contain — letterbox onto white "paper"
        scaled = img.copy()
        scaled.thumbnail((w, h), Image.Resampling.LANCZOS)
        fitted = Image.new("RGB", (w, h), (255, 255, 255))
        fitted.paste(scaled, ((w - scaled.width) // 2, (h - scaled.height) // 2))
        return fitted


def _flat_palette(colors) -> list:
    """Flatten a colour list to a padded 256-entry palette (repeat black; harmless duplicate)."""
    flat: list = []
    for rgb in colors:
        flat.extend(rgb)
    return flat + [0, 0, 0] * (256 - len(colors))


def _cached_palette_image(key: str, colors) -> Image.Image:
    """Build (once) a Pillow 'P' image carrying the palette, for quantize()."""
    if key not in _PALETTE_IMAGE_CACHE:
        pal = Image.new("P", (1, 1))
        pal.putpalette(_flat_palette(colors))
        _PALETTE_IMAGE_CACHE[key] = pal
    return _PALETTE_IMAGE_CACHE[key]


def _palette_image(name: str) -> Image.Image:
    return _cached_palette_image(name, PALETTES[name])


def _apply_gamma(img: Image.Image, gamma: float) -> Image.Image:
    """Per-channel gamma via LUT. gamma>1 darkens highlights/midtones."""
    if abs(gamma - 1.0) < 1e-3:
        return img
    lut = [round(255 * (i / 255) ** gamma) for i in range(256)]
    return img.point(lut * len(img.getbands()))


def _adaptive_gamma(img: Image.Image) -> float:
    """Bench-calibrated highlight pulldown (2026-07-19), 1.4..1.5.

    A single light ink (grey-white) means bright pieces flatten ("wash"). Pulling highlights down into
    the panel's dither range recovers structure — and helps EVERY image, not just high-key ones. But the
    amount needed is driven by flat *low-chroma near-white* content, NOT overall brightness: a woodblock
    print (big pale areas) needs more pulldown than an equally-bright but chromatic painting whose hues
    already separate. So we key gamma on the 'wash' fraction (bright AND near-neutral pixels), measured
    on a downscaled copy (cheap vs. a ~9s panel refresh).
    """
    small = img.resize((256, 256))
    r, g, b = small.split()
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    chroma = ImageChops.subtract(mx, mn)
    bright = small.convert("L").point(lambda v: 255 if v > 204 else 0)
    lowchroma = chroma.point(lambda v: 255 if v < 40 else 0)
    wash_pct = ImageChops.multiply(bright, lowchroma).histogram()[255] / (256 * 256) * 100.0
    return 1.4 + 0.1 * max(0.0, min(1.0, (wash_pct - 10.0) / 15.0))


@lru_cache(maxsize=128)
def render_for_epaper(
    image_path: Path,
    w: int,
    h: int,
    palette: str = "spectra6",
    fit: str = "cover",
    focal: tuple = (0.5, 0.5),
    fmt: str = "png",
    enhance: bool = True,
) -> bytes:
    """Render a source image to a palette-dithered bitmap sized exactly w x h.

    Cached on its arguments like get_optimized_image(); images in _Library are
    content-stable so path is a sufficient key.
    """
    if palette not in PALETTES:
        raise ValueError(f"Unknown palette '{palette}'. Options: {', '.join(PALETTES)}")
    fmt = fmt.lower()
    if fmt not in VALID_FORMATS:
        raise ValueError(f"Unknown format '{fmt}'. Options: {', '.join(VALID_FORMATS)}")
    if fit not in VALID_FITS:
        fit = "cover"

    fitted = _fit_rgb(image_path, w, h, fit, focal)

    if palette == "spectra6":
        # Bench-calibrated path (2026-07-19): adaptive highlight pulldown, then Floyd-Steinberg dither
        # toward the panel's REAL primaries, then re-encode to pure primaries so any client maps it
        # correctly. `enhance` now gates the adaptive gamma pulldown (default on). See SPECTRA6_* above.
        if enhance:
            fitted = _apply_gamma(fitted, _adaptive_gamma(fitted))
        quantized = fitted.quantize(
            palette=_cached_palette_image("_spectra6_dither", SPECTRA6_DITHER_PALETTE),
            dither=Image.Dither.FLOYDSTEINBERG,
        )
        quantized.putpalette(_flat_palette(SPECTRA6_OUTPUT_PALETTE))
    else:
        if enhance:
            # Gentle pre-boost so the tiny palette doesn't read as muddy.
            fitted = ImageEnhance.Contrast(fitted).enhance(1.12)
            if palette in _COLOR_PALETTES:
                fitted = ImageEnhance.Color(fitted).enhance(1.25)
        quantized = fitted.quantize(
            palette=_palette_image(palette), dither=Image.Dither.FLOYDSTEINBERG
        )

    buf = io.BytesIO()
    pil_format, _media = VALID_FORMATS[fmt]
    if pil_format == "BMP":
        # 24-bit RGB BMP is the broadest common denominator for firmware
        # that reads pixels and maps to its own palette. Packed/raw 1-bit
        # device buffers are deferred (v2).
        quantized.convert("RGB").save(buf, format="BMP")
    else:
        quantized.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@lru_cache(maxsize=64)
def render_fullcolor(
    image_path: Path,
    w: int,
    h: int,
    fit: str = "cover",
    focal: tuple = (0.5, 0.5),
    quality: int = 90,
) -> bytes:
    """Render a source image to a full-colour JPEG sized exactly w x h, for displays
    that want a normal image (e.g. a Samsung Frame TV's Art Mode at 3840x2160) — same
    EXIF-orient + cover/contain framing as the e-ink path, but no palette quantization
    or dithering. Cached on its arguments like render_for_epaper()."""
    fitted = _fit_rgb(image_path, w, h, fit, focal)
    buf = io.BytesIO()
    fitted.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def media_type_for(ext: str) -> str:
    """image/png or image/bmp for a requested extension."""
    return VALID_FORMATS[ext.lower()][1]
