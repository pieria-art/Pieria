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

#: Order the patches are LAID OUT in along the calibration strip. Deliberately NOT palette order.
#:
#: ⚠️ In palette order the BLACK patch sits immediately beside the WHITE one — the zero point of every
#: measurement placed next to the brightest thing on the panel. Lateral scatter in the glass then
#: lifts the anchor. Measured 2026-08-29: brightness ACROSS a single black patch climbed monotonically
#: 87.4 -> 106.0 from its far side toward the white neighbour, an 18.6/255 gradient inside one patch,
#: leaving the strip's black 11.8 brighter than a large black field of the same ink.
#:
#: That is not a cosmetic error. The affine's offset comes from this patch, so every ink DARKER than
#: the contaminated anchor is crushed to zero — measured on red's G and B and on blue's R, which is
#: exactly the "red, blue and green all measured [0,0,0] while registration was working correctly"
#: failure already on record. A deeper measurement inset only recovers 11.8 -> 7.6, because the
#: scatter spans the whole patch; the neighbours have to change.
#:
#: So the two anchors go to OPPOSITE ENDS, with the dark chromatic inks between them.
STRIP_ORDER = ("black", "blue", "green", "red", "yellow", "white")


def _output_inks() -> list:
    return [tuple(c) for c in ep.SPECTRA6_OUTPUT_PALETTE]


def _quantize(img: Image.Image, pre=None) -> Image.Image:
    """The production quantise step, so a measured pattern goes through what art goes through.

    `pre` is the render's pre-transform chain (white-point, gamma). It must run HERE, before the
    quantise, because that is where it runs on art — applying it to an already-dithered pattern
    would transform the dither instead of the content and measure nothing that ships.
    """
    if pre is not None:
        img = pre(img)
    q = img.quantize(palette=ep._cached_palette_image("_spectra6_dither", ep.SPECTRA6_DITHER_PALETTE),
                     dither=Image.Dither.FLOYDSTEINBERG)
    q.putpalette(ep._flat_palette(ep.SPECTRA6_OUTPUT_PALETTE))
    return q.convert("RGB")


def fiducial_centres(w: int, h: int) -> list:
    """Centres of the four corner fiducials, in render pixels: tl, tr, br, bl."""
    a, b = FID_INSET, FID_INSET
    return [(a, b), (w - 1 - a, b), (w - 1 - a, h - 1 - b), (a, h - 1 - b)]


def content_box(w: int, h: int) -> tuple:
    """Pixel rect of the measurable content area — inside the FIDUCIALS, not merely inside the frame.

    ⚠️ THE MEASURABLE AREA MUST NOT EXTEND BEYOND THE REGISTRATION POINTS. The homography is solved
    from the four fiducials, so inside their span it INTERPOLATES and outside it EXTRAPOLATES — and
    extrapolating a mapping that is absorbing lens distortion goes wrong fast.

    Measured 2026-08-29. The content box used to start at x=36 while the fiducials sit at x=132 and
    x=1467, so 96 px at each side were extrapolated. Vertically the content (204-894) already sat
    inside the fiducials (132-1067) and was merely interpolated. The two error scales matched that
    exactly: ~25 px of vertical slip, ~95 px of horizontal slip — a full cell width on a dense grid.

    The consequence was not subtle and was nearly banked as physics: with every sampling window one
    cell to the left of the cell it was measuring, the ink-mixture chart reported that ZERO of 15 ink
    pairs mix additively, with errors to 234/255. Two inks in a checkerboard cannot do that. The large
    `primaries` fields survived only because a 509 px cell sampled at a 0.30 inset has 153 px of
    margin to lose.

    The cost is 22% of the width (1528 -> 1192). That is the price of every measurement being taken
    where the registration is actually solved rather than guessed.
    """
    x0 = FID_INSET + FID_SIZE // 2 + FID_CLEAR
    x1 = w - FID_INSET - FID_SIZE // 2 - FID_CLEAR
    y0 = FID_INSET + FID_SIZE // 2 + FID_CLEAR
    # The patch strip sits ABOVE the bottom fiducials, not below them. Laying it out from the panel
    # edge upward put the strip straight on top of them at smaller panel sizes — the two features
    # occupied the same rows and neither could be measured.
    y1 = h - FID_INSET - FID_SIZE // 2 - FID_CLEAR - PATCH_GAP - PATCH_H
    return (x0, y0, x1, y1)


def compose(content: Image.Image, w: int, h: int, extra_patches=None,
            patches: bool = True) -> Image.Image:
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

    if not patches:
        return canvas
    # Calibration strip: the six pure inks, then any extra patches (e.g. dithered greys).
    by_name = dict(zip(INK_NAMES, _output_inks()))
    patches = [(n, by_name[n]) for n in STRIP_ORDER] + list(extra_patches or [])
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


def target_ramp(w: int, h: int, steps: int = 17, pre=None) -> Image.Image:
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
    return _quantize(img, pre)


def target_huegrid(w: int, h: int, hues: int = 12, sats: int = 6, pre=None) -> Image.Image:
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
    return _quantize(img.convert("RGB"), pre)


def target_flat(w: int, h: int) -> Image.Image:
    """A featureless white field — the FLAT-FIELD reference.

    Photograph it once per rig setup and it becomes a per-pixel map of how the light actually falls
    across the panel, plus the lens's own vignetting. Dividing later frames by it removes both.

    Necessary rather than nice: measured illumination varies by ~100/255 corner to centre on this
    rig, and the colour correction is a single global affine, so an ink sitting in a dim corner reads
    darker than the black anchor sitting in a bright one and is crushed to zero. That is exactly what
    happened — red, blue and green all measured [0,0,0] while registration was working correctly.
    """
    x0, y0, x1, y1 = content_box(w, h)
    return Image.new("RGB", (x1 - x0, y1 - y0), WHITE)


# --- helpers for the characterisation battery (2026-08-29) -----------------------------------------

#: 8x8 ordered (Bayer) matrix. Used to lay down a DETERMINISTIC ink mixture at an exact ratio —
#: deliberately not Floyd-Steinberg, because the point of the mixture chart is to know the ratio
#: exactly rather than to exercise error diffusion.
_BAYER8 = [[0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
           [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
           [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
           [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]]


def _mix_tile(wpx: int, hpx: int, a, b, frac_a: float, element: int = 2) -> Image.Image:
    """A deterministic two-ink mixture: exactly `frac_a` of the area is ink `a`, the rest ink `b`.

    ⚠️ Undithered BY CONSTRUCTION. Every tone on this panel is normally a Floyd-Steinberg mixture of
    pure inks, so the renderer's whole model rests on an assumption nobody has measured: that mixing
    ink A and ink B in a known ratio lands where the arithmetic says it will. This tile fixes the
    ratio so the panel's actual optical mixing law can be read off, with the dither taken out of the
    question entirely.

    `element` is the size of one checker square in panel px. At 1 px the mixing happens partly in the
    lens rather than in the ink; sweeping it is how that gets separated (see `target_inkmix`).
    """
    img = Image.new("RGB", (max(1, wpx), max(1, hpx)), tuple(b))
    px = img.load()
    cut = frac_a * 64.0
    for y in range(img.height):
        my = (y // element) % 8
        for x in range(img.width):
            if _BAYER8[my][(x // element) % 8] < cut:
                px[x, y] = tuple(a)
    return img


def _grid_rects(cw: int, ch: int, cols: int, rows: int, gutter: int = 8):
    """Cell rectangles for a cols x rows grid inside a content box, with a gutter between cells.

    The gutter is not cosmetic. Measurement fuses a 6x box-downscale, and a cell boundary that lands
    inside a fused pixel mixes two conditions into one number. White gutters keep every cell's
    interior unambiguous and give the 0.22 inset in `_mean_rgb` something safe to bite into.
    """
    out = []
    for r in range(rows):
        for c in range(cols):
            x0 = c * cw // cols
            x1 = (c + 1) * cw // cols
            y0 = r * ch // rows
            y1 = (r + 1) * ch // rows
            out.append((x0 + gutter, y0 + gutter, x1 - gutter, y1 - gutter))
    return out


def target_inkmix(w: int, h: int) -> Image.Image:
    """THE KEYSTONE. Every ink PAIR at five known ratios, plus an element-size sweep and a metamer test.

    Why this is the most valuable pattern in the battery: the renderer chooses inks by distance in
    sRGB against `SPECTRA6_DITHER_PALETTE`, which assumes mixtures behave additively and that
    Pimoroni's primaries describe THIS panel. Neither has been measured. This target measures the
    real mixing law directly, and because it is undithered it is a pure PANEL invariant — no render
    settings apply, so it is captured once and never swept.

    It is also what makes an offline quantiser simulator trustworthy, which is how the sweeps get
    predicted instead of photographed one refresh at a time.

    Rows 0-4  : 15 ink pairs x ratios 1:7, 1:3, 1:1, 3:1, 7:1.
    Row  5    : ELEMENT-SIZE sweep at a fixed 1:1 ratio (1, 2, 4, 8, 16 px checkers). Differences
                across pitch are panel dot-spread plus camera MTF, which sets the floor on how fine
                a dither can usefully be — and instantly reveals any rescale or re-dither downstream
                of server quantisation.
    Row  6    : METAMER pairs — different ink mixtures the renderer computes to the SAME target. If
                they measure differently on glass, additivity fails and every distance calculation
                in the quantiser is on sand.
    """
    x0, y0, x1, y1 = content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    img = Image.new("RGB", (cw, ch), WHITE)
    inks = _output_inks()
    pairs = [(i, j) for i in range(len(inks)) for j in range(i + 1, len(inks))]   # 15
    ratios = (0.125, 0.25, 0.5, 0.75, 0.875)
    cols, rows = len(pairs), 7
    rects = _grid_rects(cw, ch, cols, rows, gutter=6)

    for r, frac in enumerate(ratios):
        for c, (ia, ib) in enumerate(pairs):
            rx0, ry0, rx1, ry1 = rects[r * cols + c]
            img.paste(_mix_tile(rx1 - rx0, ry1 - ry0, inks[ia], inks[ib], frac), (rx0, ry0))

    # Row 5 — element-size sweep, 1:1 black/white repeated across the row at growing checker pitch.
    for c in range(cols):
        rx0, ry0, rx1, ry1 = rects[5 * cols + c]
        element = (1, 2, 4, 8, 16)[c % 5]
        ia, ib = ((0, 1), (1, 3), (0, 2))[c // 5]      # black/white, white/yellow, black/red
        img.paste(_mix_tile(rx1 - rx0, ry1 - ry0, inks[ia], inks[ib], 0.5, element), (rx0, ry0))

    # Row 6 — metamer pairs, laid out adjacently so a difference is visible as an edge, not only as
    # two numbers. Each triple is (ink a, ink b, fraction) chosen to predict a similar sRGB mean.
    metamers = [(1, 0, 0.5), (1, 4, 0.5), (1, 5, 0.5), (3, 4, 0.5), (3, 5, 0.5),
                (2, 1, 0.5), (2, 3, 0.5), (0, 3, 0.5), (0, 1, 0.25), (4, 1, 0.75),
                (5, 1, 0.75), (2, 0, 0.75), (3, 0, 0.75), (1, 2, 0.75), (1, 3, 0.25)]
    for c, (ia, ib, f) in enumerate(metamers):
        rx0, ry0, rx1, ry1 = rects[6 * cols + c]
        img.paste(_mix_tile(rx1 - rx0, ry1 - ry0, inks[ia], inks[ib], f), (rx0, ry0))
    return img


def target_huevalue(w: int, h: int, hues: int = 12, values: int = 6, sat: float = 0.55,
                    isolate: bool = False, pre=None) -> Image.Image:
    """Hue x VALUE grid, DITHERED — ADR-091's table, measured on glass at last.

    ⚠️ ADR-091 claims chroma survival collapses as VALUE rises: simulated, every hue survives at
    v=100 and six collapse to zero at v=220. That table was produced by simulating the quantiser and
    has NEVER been checked against the panel, yet it is the mechanism the entire white-point decision
    rests on. `target_huegrid` cannot answer it — it pins value at 200 and varies saturation, so it
    measures one row of the table the argument needs.

    `isolate` dithers each cell on its OWN, then composites. The default dithers the whole grid in
    one pass, which is what art gets. The DIFFERENCE between the two is the measurement: Floyd-
    Steinberg diffuses error across cell boundaries, so in the joint version every cell is
    contaminated by its neighbour above-left. That contamination is invisible and unquantified in the
    existing `target_huegrid`, and it bounds how much any dithered grid can be trusted.
    """
    x0, y0, x1, y1 = content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    s = round(255 * sat)
    rects = _grid_rects(cw, ch, hues, values, gutter=6)

    if isolate:
        img = Image.new("RGB", (cw, ch), WHITE)
        for r in range(values):
            v = round(40 + (245 - 40) * r / max(values - 1, 1))
            for c in range(hues):
                rx0, ry0, rx1, ry1 = rects[r * hues + c]
                cell = Image.new("HSV", (rx1 - rx0, ry1 - ry0), (round(256 * c / hues), s, v))
                img.paste(_quantize(cell.convert("RGB"), pre), (rx0, ry0))
        return img

    canvas = Image.new("HSV", (cw, ch), (0, 0, 255))
    d = ImageDraw.Draw(canvas)
    for r in range(values):
        v = round(40 + (245 - 40) * r / max(values - 1, 1))
        for c in range(hues):
            rx0, ry0, rx1, ry1 = rects[r * hues + c]
            d.rectangle([rx0, ry0, rx1 - 1, ry1 - 1], fill=(round(256 * c / hues), s, v))
    return _quantize(canvas.convert("RGB"), pre)


def target_surround(w: int, h: int, centre: int = 170, pre=None) -> Image.Image:
    """The SAME input value, 25 times, each wrapped in a different surround — DITHERED.

    A validity test for the whole dithered half of the battery rather than a curiosity. Floyd-
    Steinberg carries error into its neighbours, so a cell's measured value is a function of what
    sits around it. If these 25 identical centres do not measure the same, then no grid target
    measures "the panel's response to value V" — it measures "V given its neighbours", and every
    number read off `huegrid` or `huevalue` carries an unquantified surround term.

    The centre patch is generously inset so the readout is taken well clear of the boundary where
    the incoming error is largest.
    """
    x0, y0, x1, y1 = content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    inks = _output_inks()
    surrounds = [(v, v, v) for v in (0, 40, 80, 120, 160, 200, 255)] + inks + \
                [(255, 128, 0), (0, 128, 255), (128, 0, 255), (255, 0, 128), (0, 255, 128),
                 (128, 255, 0), (90, 90, 90), (200, 200, 200), (30, 30, 30), (230, 180, 140),
                 (60, 90, 140), (140, 60, 60)]
    img = Image.new("RGB", (cw, ch), WHITE)
    d = ImageDraw.Draw(img)
    rects = _grid_rects(cw, ch, 5, 5, gutter=4)
    for i, (rx0, ry0, rx1, ry1) in enumerate(rects[:25]):
        d.rectangle([rx0, ry0, rx1 - 1, ry1 - 1], fill=surrounds[i % len(surrounds)])
        ix = (rx1 - rx0) // 4
        iy = (ry1 - ry0) // 4
        d.rectangle([rx0 + ix, ry0 + iy, rx1 - ix - 1, ry1 - iy - 1], fill=(centre, centre, centre))
    # A high-frequency-texture surround cannot be a flat fill; give the last cell real texture.
    rx0, ry0, rx1, ry1 = rects[24]
    for yy in range(ry0, ry1):
        for xx in range(rx0, rx1):
            if (xx // 3 + yy // 3) % 2 == 0:
                img.putpixel((xx, yy), (255, 255, 255))
    ix, iy = (rx1 - rx0) // 4, (ry1 - ry0) // 4
    d.rectangle([rx0 + ix, ry0 + iy, rx1 - ix - 1, ry1 - iy - 1], fill=(centre, centre, centre))
    return _quantize(img, pre)


def target_tonefine(w: int, h: int, lo: int = 100, hi: int = 200, steps: int = 26, pre=None) -> Image.Image:
    """Dense tone steps THROUGH the white-ink ceiling, plus a low-slope gradient — DITHERED.

    Deliberately 100-200 rather than the full range. The white ink sits at luminance 163, so
    everything above input ~175 is already flat white and adding steps there measures nothing;
    below 100 the rig's ~8/255 dark accuracy is coarser than the step size. 100-200 at delta-4 puts
    every step inside the instrument's resolution and straddles the ceiling, which is exactly where
    the shipping decision lives.

    Read TWICE from one photograph: the mean of each cell is the tone response, and the local
    variance of each cell is the DITHER GRAIN — the cost that white-point compression is known to
    incur when a flat bright area drops below the ceiling and has to be built from black and white
    dots. Grain has been described by a judge but never measured.

    The step values include the midpoints between adjacent ink luminances (0, 71, 73, 101, 156, 163),
    where error diffusion is most prone to locking into periodic worms.
    """
    x0, y0, x1, y1 = content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    img = Image.new("RGB", (cw, ch), WHITE)
    d = ImageDraw.Draw(img)
    band = int(ch * 0.72)
    cols, rows = 13, 2
    for i in range(steps):
        v = round(lo + (hi - lo) * i / max(steps - 1, 1))
        c, r = i % cols, i // cols
        px0 = c * cw // cols
        px1 = (c + 1) * cw // cols
        py0 = r * band // rows
        py1 = (r + 1) * band // rows
        d.rectangle([px0 + 4, py0 + 4, px1 - 5, py1 - 5], fill=(v, v, v))
    # Low-slope gradient, 200 -> 255 across the full width: banding and contouring show here and
    # nowhere else, because the step edges of the ladder above hide them.
    for x in range(cw):
        v = round(200 + 55 * x / max(cw - 1, 1))
        d.line([(x, band), (x, ch - 1)], fill=(v, v, v))
    return _quantize(img, pre)


def target_edges(w: int, h: int, pre=None) -> Image.Image:
    """Hard edges at several contrasts, both polarities — DITHERED. The error-diffusion smear test.

    Floyd-Steinberg pushes its error to the RIGHT and BELOW, so the trailing sides of an edge carry
    a residue the leading sides do not. Reading the asymmetry of the profile across each edge
    measures that directly, and it is the thing a slanted-edge MTF target cannot do here: at
    0.86 camera px per panel px an ESF measurement returns the webcam's MTF, and error diffusion
    makes the edge stochastic where that method assumes a deterministic step.

    This is directly about art rather than about optics — every painting is edges.
    """
    x0, y0, x1, y1 = content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    img = Image.new("RGB", (cw, ch), WHITE)
    d = ImageDraw.Draw(img)
    pairs = [(30, 220), (60, 190), (90, 170), (120, 200), (140, 175), (20, 120),
             (150, 255), (100, 163)]
    rects = _grid_rects(cw, ch, 4, 2, gutter=6)
    for i, (rx0, ry0, rx1, ry1) in enumerate(rects[:8]):
        lo, hi = pairs[i]
        bg, fg = (lo, hi) if i % 2 == 0 else (hi, lo)
        d.rectangle([rx0, ry0, rx1 - 1, ry1 - 1], fill=(bg, bg, bg))
        mx, my = (rx1 - rx0) // 4, (ry1 - ry0) // 4
        d.rectangle([rx0 + mx, ry0 + my, rx1 - mx - 1, ry1 - my - 1], fill=(fg, fg, fg))
    return _quantize(img, pre)


def target_linepairs(w: int, h: int, pre=None) -> Image.Image:
    """Line pairs at coarse periods, three orientations, two contrasts — DITHERED.

    What detail survives Floyd-Steinberg. Periods stop at 8 px because below that the measurement is
    the camera: at ~0.86 camera px per panel px, an 8 px period is already close to Nyquist.

    The DIAGONAL orientation is the one that matters. Error diffusion is direction-biased — error
    travels right and down — so horizontal and vertical structure are not treated alike, and a
    diagonal exercises both at once.
    """
    x0, y0, x1, y1 = content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    img = Image.new("RGB", (cw, ch), WHITE)
    d = ImageDraw.Draw(img)
    periods = (8, 12, 16, 24, 32, 48)
    orients = ("h", "v", "d")
    contrasts = ((110, 190), (60, 240))
    rects = _grid_rects(cw, ch, 6, 6, gutter=5)
    i = 0
    for ci, (lo, hi) in enumerate(contrasts):
        for oi, orient in enumerate(orients):
            for p in periods:
                rx0, ry0, rx1, ry1 = rects[i]
                i += 1
                d.rectangle([rx0, ry0, rx1 - 1, ry1 - 1], fill=(lo, lo, lo))
                for yy in range(ry0, ry1):
                    for xx in range(rx0, rx1):
                        k = {"h": yy, "v": xx, "d": xx + yy}[orient]
                        if (k // (p // 2)) % 2 == 0:
                            img.putpixel((xx, yy), (hi, hi, hi))
    return _quantize(img, pre)


def target_uniformity(w: int, h: int) -> Image.Image:
    """Every ink repeated across nine panel positions — UNDITHERED. Spatial non-uniformity.

    ⚠️ USELESS UNLESS CAPTURED TWICE WITH THE PANEL ROTATED 180 DEGREES BETWEEN CAPTURES. Panel
    non-uniformity and the rig's own flat-field residual are otherwise perfectly confounded, and a
    single capture measures the lighting rather than the panel. Rotating the panel flips the
    panel-fixed component while leaving the rig-fixed component where it is, which separates the two
    algebraically. One capture is not a weaker version of this measurement; it is a different and
    misleading one.
    """
    x0, y0, x1, y1 = content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    img = Image.new("RGB", (cw, ch), WHITE)
    d = ImageDraw.Draw(img)
    inks = _output_inks()
    macros = _grid_rects(cw, ch, 3, 3, gutter=6)
    for mx0, my0, mx1, my1 in macros:
        mw, mh = mx1 - mx0, my1 - my0
        for i, colour in enumerate(inks):
            c, r = i % 3, i // 3
            d.rectangle([mx0 + c * mw // 3 + 2, my0 + r * mh // 2 + 2,
                         mx0 + (c + 1) * mw // 3 - 3, my0 + (r + 1) * mh // 2 - 3], fill=colour)
    return img


def target_resample(w: int, h: int, pre=None) -> Image.Image:
    """One texture presented at three source scales — DITHERED. Separates resampler loss from panel loss.

    `render_for_epaper` fits and crops before it dithers, so a master is resampled on the way in.
    When fine texture disappears from a print, "the panel cannot show it" and "the resampler already
    ate it before the dither saw it" are different diagnoses with different fixes, and nothing else
    in the battery distinguishes them. Same texture, three scales, one refresh.
    """
    x0, y0, x1, y1 = content_box(w, h)
    cw, ch = x1 - x0, y1 - y0
    img = Image.new("RGB", (cw, ch), WHITE)
    rects = _grid_rects(cw, ch, 3, 1, gutter=8)
    for si, scale in enumerate((1, 2, 4)):
        rx0, ry0, rx1, ry1 = rects[si]
        tw, th = (rx1 - rx0) * scale, (ry1 - ry0) * scale
        tile = Image.new("RGB", (tw, th), (128, 128, 128))
        td = ImageDraw.Draw(tile)
        for k in range(0, tw, 12 * scale):
            td.rectangle([k, 0, k + 6 * scale - 1, th], fill=(200, 200, 200))
        for k in range(0, th, 20 * scale):
            td.rectangle([0, k, tw, k + 5 * scale - 1], fill=(70, 70, 70))
        img.paste(tile.resize((rx1 - rx0, ry1 - ry0), Image.LANCZOS), (rx0, ry0))
    return _quantize(img, pre)


TARGETS = {
    # Panel invariants — undithered, no render settings apply, captured once and never swept.
    "primaries": target_primaries,
    "inkmix": target_inkmix,
    "uniformity": target_uniformity,
    "flat": target_flat,
    # Dithered — these exercise the render, so they are the ones worth sweeping.
    "ramp": target_ramp,
    "tonefine": target_tonefine,
    "huegrid": target_huegrid,
    "huevalue": target_huevalue,
    "surround": target_surround,
    "edges": target_edges,
    "linepairs": target_linepairs,
    "resample": target_resample,
}
