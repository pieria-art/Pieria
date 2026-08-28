"""
tools/ink_span_scan.py — find masters that are mostly PAPER, so the crop re-derivation can be targeted
(maintainer tool — NOT part of the runtime image).

WHY THIS EXISTS. ADR-087 changed what an `aspect_crops` box is for on a landscape panel: fill the
frame with the SUBJECT rather than preserve the composition. That change is only correct for works
whose master includes surrounding paper — a full-sheet Audubon scan is ~60% margin, so a
composition-preserving box fills the panel largely with blank stock. A painting scanned edge to edge
is already right and must not be touched, because "crop into the work" on one of those just discards
the artwork.

So the re-derivation needs a TARGET LIST, not a collection name. The 2026-08-01 estimate (~430 works,
~95% of them Audubon) came from a stratified 326-work sample that was never committed; this tool
scans the real library and emits the actual list.

METHOD. Decode each master small (PIL `draft`, so this is I/O-bound rather than a full decode), then
ask of every row and column: is this line blank paper? A line qualifies when it is BOTH near-uniform
(low stddev — no ink crosses it) and light (high mean — it is paper, not a dark background). Leading
and trailing runs of such lines are the margin; interior blank lines are not, because a work can have
an empty sky in the middle of it.

    python -m tools.ink_span_scan --library art-pack/_Library --out ink_span.json
    python -m tools.ink_span_scan --library art-pack/_Library --collection audubon-birds-of-america

⚠️ Scan the SAME bytes the renderer serves. `art-pack/_Library` masters are post-Tier-1 `crop_box`
(the photographed-frame trim); `_catalog_thumbs` are pre-crop and live in a different coordinate
space, so a margin measured there would not correspond to the box an agent later draws.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageStat

#: A line counts as blank paper when its stddev is below this AND its mean is above PAPER_MIN_LUM.
#: Both conditions matter: stddev alone calls a flat dark background "blank", which would trim into
#: night scenes and astro plates — the works that least want cropping.
BLANK_MAX_STDDEV = 8.0
PAPER_MIN_LUM = 200.0

#: Report a work when either axis carries more than this fraction of blank margin. 0.15 is the
#: threshold the 2026-08-01 sample used; kept so the two numbers are comparable.
MARGIN_FLAG = 0.15

SCAN_PX = 400


def _line_blank(stats_mean: float, stats_std: float) -> bool:
    return stats_std < BLANK_MAX_STDDEV and stats_mean > PAPER_MIN_LUM


def margins(img: Image.Image) -> dict:
    """Leading/trailing blank-paper fractions on each axis."""
    g = img.convert("L")
    w, h = g.size

    def run(lines):
        """(leading, trailing) counts of blank lines."""
        lead = 0
        for m, s in lines:
            if _line_blank(m, s):
                lead += 1
            else:
                break
        trail = 0
        for m, s in reversed(lines):
            if _line_blank(m, s):
                trail += 1
            else:
                break
        # An entirely blank image would double-count; clamp so fractions stay <= 1.
        if lead + trail > len(lines):
            trail = max(0, len(lines) - lead)
        return lead, trail

    cols = []
    for x in range(w):
        st = ImageStat.Stat(g.crop((x, 0, x + 1, h)))
        cols.append((st.mean[0], st.stddev[0]))
    rows = []
    for y in range(h):
        st = ImageStat.Stat(g.crop((0, y, w, y + 1)))
        rows.append((st.mean[0], st.stddev[0]))

    xl, xr = run(cols)
    yt, yb = run(rows)
    return {"left": xl / w, "right": xr / w, "top": yt / h, "bottom": yb / h,
            "x_margin": (xl + xr) / w, "y_margin": (yt + yb) / h}


def scan_one(path: Path) -> dict | None:
    try:
        with Image.open(path) as im:
            im.draft("L", (SCAN_PX, SCAN_PX))     # decode small: this is the whole speed story
            im = im.convert("L")
            im.thumbnail((SCAN_PX, SCAN_PX), Image.BILINEAR)
            m = margins(im)
    except Exception as exc:                       # a corrupt master must not abort a 3000-file scan
        print(f"  !! {path.name}: {exc}", file=sys.stderr)
        return None
    collection = path.stem.split("__")[0]
    m.update({"file": path.name, "collection": collection,
              "flagged": max(m["x_margin"], m["y_margin"]) > MARGIN_FLAG})
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", default="art-pack/_Library")
    ap.add_argument("--collection", default="", help="restrict to one collection prefix")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="ink_span.json")
    args = ap.parse_args()

    lib = Path(args.library)
    if not lib.is_dir():
        sys.exit(f"no library at {lib}")
    paths = sorted(p for p in lib.glob("*.jpg")
                   if not args.collection or p.stem.startswith(args.collection + "__"))
    if args.limit:
        paths = paths[:args.limit]
    print(f"scanning {len(paths)} masters in {lib}")

    results, by_coll = [], {}
    for i, p in enumerate(paths, 1):
        r = scan_one(p)
        if r is None:
            continue
        results.append(r)
        c = by_coll.setdefault(r["collection"], {"n": 0, "flagged": 0})
        c["n"] += 1
        c["flagged"] += 1 if r["flagged"] else 0
        if i % 250 == 0:
            print(f"  {i}/{len(paths)}")

    flagged = [r for r in results if r["flagged"]]
    Path(args.out).write_text(json.dumps(
        {"threshold": MARGIN_FLAG, "scanned": len(results), "flagged": len(flagged),
         "by_collection": by_coll, "targets": sorted(flagged, key=lambda r: r["file"])}, indent=1))

    print(f"\n{len(flagged)}/{len(results)} flagged (> {MARGIN_FLAG:.0%} blank margin on an axis)")
    print(f"\n{'collection':38s} {'flagged':>8s} {'total':>7s} {'pct':>6s}")
    for c, v in sorted(by_coll.items(), key=lambda kv: -kv[1]["flagged"]):
        if v["flagged"]:
            print(f"{c:38s} {v['flagged']:8d} {v['n']:7d} {100*v['flagged']/v['n']:5.0f}%")
    print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
