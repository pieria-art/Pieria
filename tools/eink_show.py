"""
tools/eink_show.py — render ANY library image to the panel at a given recipe
(maintainer tool — NOT part of the runtime image).

`eink_bench full N` can only reach the frozen 60-work bench corpus. This reaches anything in the
library, which is what you need when a specific painting misbehaves and it is not one of the sixty.

    sudo python3 -m tools.eink_show Artwork/_Library/dutch-golden-age__the-night-watch__*.jpg \
        --gamma 1.0 --white-point 0.88

The lever chain is IDENTICAL to eink_bench.cmd_full — white-point, chroma, saturation, contrast,
gamma, then quantise — so what you see here is what that recipe would put on a wall. Framing uses the
work's stored crop and focal point when the database knows it, so it matches production rather than
being a centred guess.

⚠️ GAMMA BELOW 1.0 LIFTS SHADOWS, and is the one direction the 2026-08-29 corpus never tested — its
axis was 1.0/1.4/1.8/2.2, all of which darken or leave neutral. That matters for dark paintings:
white-point compression multiplies every input by wp, so it pushes shadows DOWN and makes black crush
worse, monotonically (measured on The Night Watch: 67.4% of the shadow region renders as bare black
ink at wp 1.0, rising to 78.1% at wp 0.64). And the lever barely matters anyway — only FOUR distinct
inks appear in the shadow region at any white-point, because the palette has exactly one ink below
luminance 71 (black itself; blue is 71, green 73, red 101). Shadow modelling on this panel is
structurally starved, which is the mirror of ADR-091's finding at the light end.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageEnhance

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import eink_bench as eb  # noqa: E402
from tools import eink_calibrate as ec  # noqa: E402
from tools import eink_target as et  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--gamma", type=float, default=1.0,
                    help="＞1 darkens, ＜1 LIFTS SHADOWS (untested by the 2026-08-29 corpus)")
    ap.add_argument("--white-point", type=float, default=0.0)
    ap.add_argument("--chroma-gamma", type=float, default=1.0)
    ap.add_argument("--saturation", type=float, default=1.0)
    ap.add_argument("--contrast", type=float, default=1.0)
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1200)
    ap.add_argument("--fit", default="cover", choices=("cover", "contain"))
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    src = Path(args.image)
    if not src.exists():
        sys.exit(f"no such image: {src}")
    w, h = args.width, args.height

    crop, focal = eb._db_crop_and_focal(src.name, w, h)
    fitted = ec.epaper._fit_rgb(src, w, h, args.fit, focal, crop)

    if args.white_point > 0:
        fitted = fitted.point(
            list(ec.epaper._tone_lut(args.white_point, 1.0)) * 3)  # ADR-098: one definition of the white-point LUT
    if abs(args.chroma_gamma - 1.0) > 1e-3:
        hue, sat, val = fitted.convert("HSV").split()
        lut = [min(255, int(round(255.0 * (i / 255.0) ** args.chroma_gamma))) for i in range(256)]
        fitted = Image.merge("HSV", (hue, sat.point(lut), val)).convert("RGB")
    if abs(args.saturation - 1.0) > 1e-3:
        fitted = ImageEnhance.Color(fitted).enhance(args.saturation)
    if abs(args.contrast - 1.0) > 1e-3:
        fitted = ImageEnhance.Contrast(fitted).enhance(args.contrast)
    if args.gamma > 0:
        fitted = ec.epaper._apply_gamma(fitted, args.gamma)

    # The UNQUANTISED reference, at this exact framing, so the browser harness can show what the
    # render is trying to be. It has to be produced here rather than reused: the crop and focal point
    # come from the database per work, so a reference generated any other way is a different picture.
    ref_dir = eb.OUT / "reference"
    ref_dir.mkdir(parents=True, exist_ok=True)
    ref_name = f"show_{src.stem[:40]}.jpg"
    ec.epaper._fit_rgb(src, w, h, args.fit, focal, crop).save(ref_dir / ref_name, "JPEG", quality=92)

    out = et._quantize(fitted)
    tag = f"{src.stem[:34]}_g{args.gamma}_wp{args.white_point}_k{args.chroma_gamma}_s{args.saturation}"
    dest = eb.OUT / f"show_{tag}.png"
    eb.OUT.mkdir(parents=True, exist_ok=True)
    out.save(dest)
    print(f"{src.name}\n  crop {crop}  focal {focal}\n  {dest}")
    print(f"  reference -> bench-eink/reference/{ref_name}")
    if args.no_push:
        return
    from inky.auto import auto  # noqa: PLC0415
    panel = auto()
    pw, ph = panel.resolution
    shown = out if (out.width, out.height) == (pw, ph) else out.rotate(90, expand=True)
    panel.set_image(shown)
    panel.show()
    print("  pushed to panel")


if __name__ == "__main__":
    main()
