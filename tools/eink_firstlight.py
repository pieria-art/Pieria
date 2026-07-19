#!/usr/bin/env python3
"""
E-ink first-light + tuning bench instrument (Track B) — Pimoroni Inky Impression 13.3" Spectra 6.

Run on the bench Pi with the Inky HAT seated (SPI + I2C enabled, `inky` installed). It covers the
manual bring-up steps in `.ai/spec_eink_spectra6.md` §5.5 (first light + colour bar + refresh timing)
and §5.6 (palette/saturation tuning) so they're one command instead of hand-typed at the bench.

Laptop-safe: with `--save-dir DIR` (and no panel present) it writes the exact PNGs it *would* blit, so
the framing/dither can be eyeballed before the hardware is in hand — same "verify without hardware"
posture as the rest of Track B. Blitting is skipped automatically if `inky` can't be imported.

The two pipelines it can compare answer the open §4 question — the app's endpoint already
Floyd-Steinberg-dithers to the spectra6 anchors (`render_for_epaper`), yet `inky.set_image` *also* maps
to the panel palette; which order reads best on real glass is a bench call:
  --pipeline server : render_for_epaper (fit + enhance + FS dither) -> blit. The production path
                      (this is exactly what the /display/{id}/current.png endpoint + InkyClient do).
  --pipeline inky   : fit only (no app dither) -> hand full-colour to inky.set_image(saturation=..),
                      letting the inky driver own the palette mapping. Isolates inky's native quality.

Examples (on the Pi):
  tools/eink_firstlight.py colorbar
  tools/eink_firstlight.py show /path/to/art.jpg --pipeline server --enhance
  tools/eink_firstlight.py sweep /path/to/art.jpg --hold 8            # saturation sweep, 8s per frame
  tools/eink_firstlight.py sweep /path/to/art.jpg --pipeline server --enhance-sweep
Laptop dry check (no HAT):
  tools/eink_firstlight.py sweep /path/to/art.jpg --save-dir /tmp/eink --no-blit
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

# Import the app's render pipeline from the repo root (this tool ships in tools/ but has no app/DB deps).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from epaper import PALETTES, _fit_rgb, render_for_epaper  # noqa: E402

SPECTRA6 = PALETTES["spectra6"]
SPECTRA6_NAMES = ["black", "white", "red", "yellow", "blue", "green"]
DEFAULT_SATURATIONS = [0.0, 0.25, 0.5, 0.75, 1.0]
DEFAULT_ENHANCE_LABELS = [("no-enhance", False), ("enhance", True)]


def get_panel():
    """Import + auto-detect the Inky panel. Raises if `inky` isn't installed (caller handles)."""
    from inky.auto import auto  # lazy: hardware-only dep
    return auto()


def make_colorbar(w: int, h: int) -> Image.Image:
    """A native w×h test pattern: the six spectra6 primaries as vertical bars over the top 3/4, and a
    black→white gradient across the bottom 1/4 (to judge dither texture + the grey ramp on real glass)."""
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    n = len(SPECTRA6)
    bar_h = h * 3 // 4
    for i, rgb in enumerate(SPECTRA6):
        x0 = i * w // n
        x1 = (i + 1) * w // n
        d.rectangle([x0, 0, x1, bar_h], fill=rgb)
    for x in range(w):
        v = round(255 * x / max(1, w - 1))
        d.line([(x, bar_h), (x, h)], fill=(v, v, v))
    return img


def render_image(path: str, w: int, h: int, pipeline: str, enhance: bool, focal, portrait: bool) -> Image.Image:
    """Produce the exact image to blit for `path` under the chosen pipeline. Portrait renders at the
    swapped size then rotates 90° (expand) to fill the physically-landscape panel — matching
    eink_client.push_once's portrait handling."""
    rw, rh = (h, w) if portrait else (w, h)
    if pipeline == "server":
        data = render_for_epaper(Path(path), rw, rh, palette="spectra6",
                                 fit="cover", focal=focal, fmt="png", enhance=enhance)
        img = Image.open(io.BytesIO(data))
        img.load()
    else:  # inky pipeline — fit only, hand full colour to the driver
        img = _fit_rgb(Path(path), rw, rh, "cover", focal)
    if portrait:
        img = img.rotate(90, expand=True)
    return img


def emit(img: Image.Image, panel, saturation: float, label: str, save_dir: Path | None, hold: float) -> None:
    """Blit `img` to the panel (timing the refresh) and/or save it, then hold for photographing."""
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        out = save_dir / f"{label}.png"
        img.save(out)
        print(f"  saved {out}")
    if panel is not None:
        t0 = time.time()
        panel.set_image(img, saturation=saturation)
        panel.show()
        print(f"  blit {label}: refresh {time.time() - t0:.1f}s (saturation={saturation})")
        if hold:
            time.sleep(hold)
    elif save_dir is None:
        print("  (no panel and no --save-dir: nothing to do)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Inky Impression Spectra 6 first-light + tuning bench tool")
    ap.add_argument("command", choices=["colorbar", "show", "sweep"])
    ap.add_argument("path", nargs="?", help="source image (required for show/sweep)")
    ap.add_argument("--w", type=int, default=1600)
    ap.add_argument("--h", type=int, default=1200)
    ap.add_argument("--portrait", action="store_true", help="render 1200×1600 then rotate 90° to fill the panel")
    ap.add_argument("--pipeline", choices=["server", "inky"], default="inky",
                    help="server = render_for_epaper (production path); inky = fit only, let inky dither")
    ap.add_argument("--enhance", action="store_true", help="apply epaper enhance pre-boost (server pipeline)")
    ap.add_argument("--saturation", type=float, default=0.5, help="inky saturation for show/colorbar (0-1)")
    ap.add_argument("--focal", default="0.5,0.5", help="crop anchor x,y in 0..1 (default centre)")
    ap.add_argument("--enhance-sweep", action="store_true",
                    help="sweep: vary epaper enhance on/off instead of inky saturation (forces --pipeline server)")
    ap.add_argument("--hold", type=float, default=6.0, help="seconds to pause after each blit (for photos)")
    ap.add_argument("--save-dir", type=Path, help="also write the blitted PNG(s) here (laptop-safe preview)")
    ap.add_argument("--no-blit", action="store_true", help="never touch the panel (preview/save only)")
    args = ap.parse_args(argv)

    if args.command in ("show", "sweep") and not args.path:
        ap.error(f"{args.command} requires an image path")
    try:
        fx, fy = (float(v) for v in args.focal.split(","))
        focal = (fx, fy)
    except ValueError:
        ap.error("--focal must be 'x,y' in 0..1, e.g. 0.5,0.4")

    panel = None
    if not args.no_blit:
        try:
            panel = get_panel()
            print(f"panel: {panel.resolution} ({type(panel).__name__})")
        except Exception as e:  # noqa: BLE001 — laptop / no HAT: fall back to save-only
            print(f"no inky panel ({e}); running save-only. Use --save-dir to keep output.")

    if args.command == "colorbar":
        img = make_colorbar(args.w, args.h)
        emit(img, panel, args.saturation, "colorbar", args.save_dir, args.hold)

    elif args.command == "show":
        img = render_image(args.path, args.w, args.h, args.pipeline, args.enhance, focal, args.portrait)
        label = f"show-{args.pipeline}{'-enhance' if args.enhance else ''}"
        emit(img, panel, args.saturation, label, args.save_dir, args.hold)

    else:  # sweep
        if args.enhance_sweep:
            print("sweep: epaper enhance on/off (server pipeline)")
            for label, en in DEFAULT_ENHANCE_LABELS:
                img = render_image(args.path, args.w, args.h, "server", en, focal, args.portrait)
                emit(img, panel, args.saturation, f"sweep-{label}", args.save_dir, args.hold)
        else:
            print(f"sweep: inky saturation {DEFAULT_SATURATIONS} ({args.pipeline} pipeline)")
            img = render_image(args.path, args.w, args.h, args.pipeline, args.enhance, focal, args.portrait)
            for sat in DEFAULT_SATURATIONS:
                emit(img, panel, sat, f"sweep-sat{sat}", args.save_dir, args.hold)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
