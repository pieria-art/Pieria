"""
tools/eink_push.py — push a single rendered PNG to the panel (maintainer tool, Pi-only).
NOT part of the runtime image; NOT the same thing as `eink_client.py` (the always-on pull loop) —
this is a one-shot manual push, for judging a `tools.eink_candidate` session frame by frame.

    sudo python3 -m tools.eink_push bench-eink/analysis/session_2026-09-20/sunflowers/A_shipping.png
    sudo python3 -m tools.eink_push ... --wait

The image is prepared for `set_image()` EXACTLY the way `eink_bench.cmd_full` prepares what it
pushes: converted to RGB, then rotated 90 degrees if its dimensions don't match the panel's native
`resolution` (a portrait composition rendered onto a physically landscape buffer, same as
`EINK_ORIENTATION=portrait` does client-side) — so a candidate PNG from `tools.eink_candidate` pushes
identically to how production would have shown it.

`inky` is a Pi-only dependency (SPI + the vendor driver) and is never installed off the panel — this
module does not import it at module load time, only inside `push()`, so it can still be imported (for
`--help`, or by a test that monkeypatches the import) on a laptop with no `inky` present.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from PIL import Image

#: How long a full refresh takes (ADR-113: ~22s, paced not rationed). `--wait` sleeps past this after
#: `show()` returns, for a caller scripting several pushes in a row that must not race the panel — inky
#: drivers vary in whether `show()` itself blocks for the refresh, so this is a floor, not a substitute
#: for a real busy-pin wait (unverified without the Pi; see the module docstring in eink_candidate.py).
REFRESH_SECONDS = 25


def push(path: Path, wait: bool = False) -> None:
    try:
        from inky.auto import auto  # noqa: PLC0415 — Pi-only dependency
    except ImportError:
        print("`inky` is not installed. This tool is Pi-only: run it on the panel Pi "
              "(pip install inky, or activate the Pi's venv where it's already present).")
        sys.exit(2)

    img = Image.open(path)
    panel = auto()
    pw, ph = panel.resolution
    shown = img.convert("RGB")
    if (shown.width, shown.height) != (pw, ph):
        shown = shown.rotate(90, expand=True)
        print(f"  rotated {img.width}x{img.height} -> {shown.width}x{shown.height} for the panel "
              f"(turn the panel 90 degrees)")
    panel.set_image(shown)
    panel.show()
    if wait:
        time.sleep(REFRESH_SECONDS)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  pushed {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("png", help="path to a panel-ready PNG (e.g. from tools.eink_candidate)")
    ap.add_argument("--wait", action="store_true",
                    help=f"sleep {REFRESH_SECONDS}s after show() so the refresh has finished before returning")
    args = ap.parse_args()
    path = Path(args.png)
    if not path.exists():
        sys.exit(f"no such file: {path}")
    push(path, wait=args.wait)


if __name__ == "__main__":
    main()
