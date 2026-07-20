"""
E-paper rendering for the stateless per-display image endpoint (Track B).

Crops/fits a source image to an exact panel size and quantizes it to a fixed
device palette with Floyd–Steinberg dithering, so a "dumb" frame (DIY ESP32 +
Waveshare, Inky Impression, a TRMNL in BYOS mode, etc.) can fetch a ready-to-blit
image over HTTP without running the JS Canvas app.

No dithering existed in the app before this; see ROADMAP.md (Track B).
"""

import io
import math
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

# Per-aspect crop presets: "how do I best fill THIS screen shape with this work?" Authored per work
# (one normalized box per common panel shape) because a focal point can only SLIDE a fixed-size
# window — it can't CHOOSE one. Museum art clusters near-square, so cropping to a 9:16 panel discards
# ~53% of the median work; an explicit box lets the composition be picked instead of computed.
#
# NOT to be confused with build_pack's Tier-1 `crop_box`/`needs_frame_crop`, which answers a different
# question one level up — "what IS the artwork" (trim the photographed frame/mat/wall) — and is baked
# into the master bytes at pack build. By the time we render here, Tier 1 is already resolved.
ASPECT_CROP_KEYS = ("16:9", "9:16", "4:3", "3:4")


def normalize_crop_box(crop_box) -> tuple | None:
    """Validate a normalized art rectangle [x0,y0,x1,y1] in 0..1 -> tuple, else None.

    Shared by the renderer and build_pack's Tier-1 frame-crop so the two can't drift on what counts
    as a valid box. A near-full-frame box is deliberately None ("already fills the frame" — agents
    return [0,0,1,1] to mean that), which keeps it a true no-op rather than a pointless re-encode.
    """
    if not (isinstance(crop_box, (list, tuple)) and len(crop_box) == 4):
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in crop_box)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        return None
    if x0 <= 0.002 and y0 <= 0.002 and x1 >= 0.998 and y1 >= 0.998:
        return None
    return (x0, y0, x1, y1)


def pick_crop_for_aspect(crops, w: int, h: int) -> tuple | None:
    """Choose the authored crop whose key ratio is nearest the requested w:h, or None.

    Nearest-by-ratio (not exact-match) so an odd panel — an 1872x1404 e-ink, a 5:4 monitor — still
    gets the closest sensible preset instead of silently falling back to a focal cover. Compared in
    log space so 16:9-vs-4:3 and 9:16-vs-3:4 are judged by proportional, not absolute, distance.
    """
    if not isinstance(crops, dict) or not crops or w <= 0 or h <= 0:
        return None
    target = w / h
    best, best_dist = None, None
    for key, box in crops.items():
        try:
            kw, kh = (float(v) for v in str(key).split(":"))
            ratio = kw / kh
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if ratio <= 0:
            continue
        dist = abs(math.log(target / ratio))
        if best_dist is None or dist < best_dist:
            box = normalize_crop_box(box)
            if box is not None:
                best, best_dist = box, dist
    return best


_PALETTE_IMAGE_CACHE: dict = {}


def _fit_rgb(image_path: Path, w: int, h: int, fit: str = "cover",
             focal: tuple = (0.5, 0.5), crop_box=None) -> Image.Image:
    """Open a source image, honour EXIF orientation, normalise ANY input mode
    (JPEG/PNG/WebP, incl. CMYK / RGBA / palette / greyscale) to RGB, and fit it to
    exactly w x h — cover-crop (anchored on the normalized focal point, default
    centered) or contain (letterbox onto white).

    An optional normalized `crop_box` pre-crops to an authored region first; since that box is
    authored AT the target aspect the subsequent fit is near-lossless, and focal stops mattering.
    Absent/invalid box -> the focal path below, unchanged, so zero crop data is a true no-op.

    Shared by the e-ink renderer and the full-colour (Frame TV) renderer so the
    orient/crop behaviour can't drift between outputs.
    """
    if fit not in VALID_FITS:
        fit = "cover"
    box = normalize_crop_box(crop_box)
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
        if box is not None:
            iw, ih = img.size
            left, upper = round(box[0] * iw), round(box[1] * ih)
            right, lower = round(box[2] * iw), round(box[3] * ih)
            # a degenerate box on a tiny source would crop to nothing — keep the full frame instead
            if right - left >= 1 and lower - upper >= 1:
                img = img.crop((left, upper, right, lower))
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
    crop_box: tuple | None = None,
) -> bytes:
    """Render a source image to a palette-dithered bitmap sized exactly w x h.

    Cached on its arguments like get_optimized_image(); images in _Library are
    content-stable so path is a sufficient key. NOTE: because of that cache every argument must be
    hashable — pass `crop_box` as a TUPLE, never the list it arrives as from JSON.
    """
    if palette not in PALETTES:
        raise ValueError(f"Unknown palette '{palette}'. Options: {', '.join(PALETTES)}")
    fmt = fmt.lower()
    if fmt not in VALID_FORMATS:
        raise ValueError(f"Unknown format '{fmt}'. Options: {', '.join(VALID_FORMATS)}")
    if fit not in VALID_FITS:
        fit = "cover"

    fitted = _fit_rgb(image_path, w, h, fit, focal, crop_box)

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
    crop_box: tuple | None = None,
) -> bytes:
    """Render a source image to a full-colour JPEG sized exactly w x h, for displays
    that want a normal image (e.g. a Samsung Frame TV's Art Mode at 3840x2160) — same
    EXIF-orient + cover/contain framing as the e-ink path, but no palette quantization
    or dithering. Cached on its arguments like render_for_epaper() — so `crop_box` must
    be a hashable TUPLE."""
    fitted = _fit_rgb(image_path, w, h, fit, focal, crop_box)
    buf = io.BytesIO()
    fitted.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def media_type_for(ext: str) -> str:
    """image/png or image/bmp for a requested extension."""
    return VALID_FORMATS[ext.lower()][1]
