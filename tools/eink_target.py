"""
tools/eink_target.py — self-calibrating measurement targets for the e-ink panel
(maintainer tool — NOT part of the runtime image).

WHY THIS EXISTS. Every calibration judgement so far has been ordinal and human: "B is closer than A."
That caps throughput at a person standing in front of a panel, cannot be replayed, and carries the
noise of sequential viewing (the comparison is made from memory across a ~15 s refresh). A PHOTOGRAPH
of the panel is a machine-readable observation of the actual output, so trueness can be computed
against the reference instead of reported.

THE PHOTO IS NOT TRUSTWORTHY BY ITSELF, AND THE FIX IS IN THE FRAME. Room light, viewing angle and
the phone's auto white balance all distort the capture, and they change between shots — so a single
"camera offset" measured once would be wrong by the next photograph. Instead every target carries its
own calibration furniture:

  * a solid BLACK REGISTRATION FRAME, whose four inside corners give the homography that undoes
    perspective and maps the photo back onto the render's pixel grid;
  * a PATCH STRIP of the six panel inks laid down as PURE ink (never dithered), which lets a
    per-channel correction be solved from that same photograph;
  * BLACK and WHITE patches in the same strip, anchoring the tonal range.

So each photograph is normalised by itself, and two photographs taken minutes apart under different
light remain comparable. We are ranking recipes rather than doing absolute colorimetry, and a
relative comparison survives a great deal of camera error once both frames are anchored this way.

⚠️ THE PATCHES ALSO ANSWER AN OPEN QUESTION. `epaper.SPECTRA6_DITHER_PALETTE` is Pimoroni's measured
EL133UF1 primaries — someone else's panel. Every distance calculation in the renderer assumes them.
Photographing pure ink patches measures THIS panel, and if the numbers differ the whole chain
inherits the error.

⚠️ A PATTERN IS NOT ART (the ADR-084 caution, one level over). Floyd–Steinberg's output depends on
neighbouring content, so flat patches do not exercise error diffusion the way a painting does.
Patterns characterise the PANEL; the `art` target characterises the RENDER. Both are needed, and
neither substitutes for the other.

    sudo python3 -m tools.eink_bench target primaries
    sudo python3 -m tools.eink_bench target ramp
    sudo python3 -m tools.eink_bench target huegrid
    sudo python3 -m tools.eink_bench target art --n 16 --gamma 1.4 --chroma-gamma 2.0 ...
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import epaper as ep  # noqa: E402

# Geometry (panel pixels). The frame is deliberately thick: a thin line survives neither the panel's
# dot pitch nor a handheld photograph, and the whole registration depends on finding it reliably.
OUTER_MARGIN = 10          # white gutter outside the frame, so the frame never touches the bezel
FRAME_W = 16               # black registration frame
INNER_PAD = 10             # white gutter inside the frame
PATCH_H = 96               # calibration strip height
PATCH_GAP = 6

#: Corner fiducials, INBOARD of the frame. The registration frame alone is not enough on real
#: hardware: the panel's own BEZEL is dark and sits immediately outside it, so a detector looking for
#: "the outermost dark rectangle" locks onto the bezel and every measurement is offset by its width.
#: That happened — the rectified image came back containing the Pimoroni silkscreen and the flex
#: cable. Solid squares set well inside the white margin have no dark neighbour to be confused with,
#: and their centres are known exactly.
FID_SIZE = 56
FID_INSET = 132            # centre offset from the panel edge, both axes
#: Clearance the content must keep from a fiducial. The detector searches a window around each
#: fiducial, so anything dark that creeps into that window drags the centroid — measured 55 px and
#: then 87 px off when a black content cell crowded one. Content is kept outside the search window
#: by construction rather than the detector being asked to be clever about it.
FID_CLEAR = 44

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

#: Output-palette RGB for each ink, in SPECTRA6_DITHER_PALETTE order. Patches are laid down in the
#: OUTPUT palette (pure primaries) because that is what a client blits and what the panel converts to
#: ink — the same contract as ADR-053's re-encode. Never dither a calibration patch: the point is to
#: photograph one ink at a time.
INK_NAMES = ("black", "white", "red", "yellow", "blue", "green")


def _output_inks() -> list:
    return [tuple(c) for c in ep.SPECTRA6_OUTPUT_PALETTE]


def _quantize(img: Image.Image) -> Image.Image:
    """The production quantise step, so a measured pattern goes through what art goes through."""
    q = img.quantize(palette=ep._cached_palette_image("_spectra6_dither", ep.SPECTRA6_DITHER_PALETTE),
                     dither=Image.Dither.FLOYDSTEINBERG)
    q.putpalette(ep._flat_palette(ep.SPECTRA6_OUTPUT_PALETTE))
    return q.convert("RGB")


def fiducial_centres(w: int, h: int) -> list:
    """Centres of the four corner fiducials, in render pixels: tl, tr, br, bl."""
    a, b = FID_INSET, FID_INSET
    return [(a, b), (w - 1 - a, b), (w - 1 - a, h - 1 - b), (a, h - 1 - b)]


def content_box(w: int, h: int) -> tuple:
    """Pixel rect of the measurable content area, inside the frame and above the patch strip."""
    x0 = OUTER_MARGIN + FRAME_W + INNER_PAD
    x1 = w - OUTER_MARGIN - FRAME_W - INNER_PAD
    y0 = FID_INSET + FID_SIZE // 2 + FID_CLEAR
    # The patch strip sits ABOVE the bottom fiducials, not below them. Laying it out from the panel
    # edge upward put the strip straight on top of them at smaller panel sizes — the two features
    # occupied the same rows and neither could be measured.
    y1 = h - FID_INSET - FID_SIZE // 2 - FID_CLEAR - PATCH_GAP - PATCH_H
    return (x0, y0, x1, y1)


def compose(content: Image.Image, w: int, h: int, extra_patches=None) -> Image.Image:
    """Put already-quantised content inside the registration frame and add the calibration strip.

    Composition happens AFTER quantisation on purpose. Re-quantising the finished canvas would
    dither the calibration patches, and a dithered patch measures the dither rather than the ink.
    """
    canvas = Image.new("RGB", (w, h), WHITE)
    d = ImageDraw.Draw(canvas)

    # Registration frame — its INSIDE corners are the homography reference points.
    d.rectangle([OUTER_MARGIN, OUTER_MARGIN, w - 1 - OUTER_MARGIN, h - 1 - OUTER_MARGIN],
                outline=BLACK, width=FRAME_W)

    x0, y0, x1, y1 = content_box(w, h)
    fitted = content.resize((x1 - x0, y1 - y0), Image.NEAREST) if content.size != (x1 - x0, y1 - y0) \
        else content
    canvas.paste(fitted, (x0, y0))

    for fx, fy in fiducial_centres(w, h):
        d.rectangle([fx - FID_SIZE // 2, fy - FID_SIZE // 2,
                     fx + FID_SIZE // 2, fy + FID_SIZE // 2], fill=BLACK)

    # Calibration strip: the six pure inks, then any extra patches (e.g. dithered greys).
    patches = [(n, c) for n, c in zip(INK_NAMES, _output_inks())] + list(extra_patches or [])
    sy0 = y1 + PATCH_GAP
    sy1 = sy0 + PATCH_H
    total = x1 - x0
    pw = (total - PATCH_GAP * (len(patches) - 1)) / len(patches)
    for i, (_name, colour) in enumerate(patches):
        px0 = int(round(x0 + i * (pw + PATCH_GAP)))
        px1 = int(round(px0 + pw))
        if isinstance(colour, Image.Image):
            canvas.paste(colour.resize((px1 - px0, PATCH_H), Image.NEAREST), (px0, sy0))
        else:
            d.rectangle([px0, sy0, px1 - 1, sy1 - 1], fill=colour)
    return canvas


def target_primaries(w: int, h: int) -> Image.Image:
    """Large pure-ink fields. Answers: what does THIS panel actually produce for each ink?

    Big flat areas rather than small chips, because a handheld photograph needs a region whose
    interior is safely free of edge effects and of the specular highlight that a phone flash or a
    ceiling lamp puts somewhere on the glass.
    """
    x0, y0, x1, y1 = content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    img = Image.new("RGB", (cw, ch), WHITE)
    d = ImageDraw.Draw(img)
    inks = _output_inks()
    cols = 3
    rows = (len(inks) + cols - 1) // cols
    for i, colour in enumerate(inks):
        cx, cy = i % cols, i // cols
        d.rectangle([cx * cw // cols, cy * ch // rows,
                     (cx + 1) * cw // cols - 1, (cy + 1) * ch // rows - 1], fill=colour)
    return img


def target_ramp(w: int, h: int, steps: int = 17) -> Image.Image:
    """Neutral tone ramp, DITHERED — the tone-response measurement.

    This is the gamma lever expressed as something measurable: photograph it and read off where
    highlight steps stop separating. Six works in the 2026-08-28 session reported pale detail
    washing out (a mantle, engraved linework, caption text, snowflakes); this says at which input
    luminance that begins, instead of describing it.

    The ramp is built in LINEAR steps of input value and then run through the real quantiser, so
    what is photographed is what the renderer would do to a smooth gradient in a painting.
    """
    x0, y0, x1, y1 = content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    img = Image.new("RGB", (cw, ch), WHITE)
    d = ImageDraw.Draw(img)
    band = ch // 2
    for i in range(steps):
        v = round(255 * i / (steps - 1))
        d.rectangle([i * cw // steps, 0, (i + 1) * cw // steps - 1, band - 1], fill=(v, v, v))
    # Second band: a CONTINUOUS gradient, which shows banding and the clipping point without the
    # step edges to hide behind.
    for x in range(cw):
        v = round(255 * x / max(cw - 1, 1))
        d.line([(x, band), (x, ch - 1)], fill=(v, v, v))
    return _quantize(img)


def target_huegrid(w: int, h: int, hues: int = 12, sats: int = 6) -> Image.Image:
    """Hue x saturation grid, DITHERED — the gamut-survival map.

    Answers in one photograph what cost four panel renders to learn about one colour: which faint
    colours survive the dither and which collapse to grey. The 2026-08-28 session found a woodblock's
    pale blue (hue 117.6, saturation 0.143) rendering as "more gray than blue" while warm tones were
    served well; this maps that across the whole circle instead of one cell at a time.

    Rows are saturation from faint to full, columns are hue. Value is held at a light-midtone where
    the panel has the most room, so the grid measures chroma behaviour rather than tone behaviour —
    that is the ramp's job.
    """
    x0, y0, x1, y1 = content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    img = Image.new("HSV", (cw, ch))
    d = ImageDraw.Draw(img)
    for r in range(sats):
        s = round(255 * (r + 1) / sats)
        for c in range(hues):
            hue = round(256 * c / hues)
            d.rectangle([c * cw // hues, r * ch // sats,
                         (c + 1) * cw // hues - 1, (r + 1) * ch // sats - 1],
                        fill=(hue, s, 200))
    return _quantize(img.convert("RGB"))


TARGETS = {"primaries": target_primaries, "ramp": target_ramp, "huegrid": target_huegrid}
