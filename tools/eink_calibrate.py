"""E-ink render calibration harness — bench tool for dialling in the default "auto".

WHY THIS EXISTS
---------------
`epaper._adaptive_gamma()` was calibrated on the 2026-07-19 bench set (ADR-053) and washes out on a
wider corpus. It returns only **1.4–1.5**, keyed on a single feature (`wash_pct` — pixels that are both
bright and near-neutral), with four hardcoded constants. Two things need answering on real hardware,
and only one of them is "re-tune the numbers":

  1. Is `wash_pct` still the right PREDICTOR across dark oils, line art, photographs, watercolour?
  2. Is a 0.1-wide output range wide enough at all?

A panel refresh is ~9s, so bench time is the scarce resource. This tool front-loads everything that
doesn't need the panel: choosing an adversarial corpus, computing candidate predictors, and rendering
the whole parameter grid into labelled contact sheets so one refresh shows six candidates instead of one.

FIDELITY RULE
-------------
Every tile is produced by the SAME quantize path as production (`epaper.SPECTRA6_DITHER_PALETTE` →
`SPECTRA6_OUTPUT_PALETTE`, Floyd–Steinberg). A calibration harness that renders differently from what
ships is worse than no harness — you'd tune against an image the user never sees. Labels are drawn
after quantization in pure black/white, both of which are already in the output palette, so a sheet is
still panel-legal and needs no second quantize pass.

WHERE EACH KNOB ACTUALLY ACTS — the thing that makes this confusing
-------------------------------------------------------------------
WE do the render. `render_for_epaper` dithers to the measured primaries and re-encodes to PURE
primaries server-side; that replaced inky's native dithering path (calibration doc §3). But on a
Pimoroni Inky, inky is still the TRANSPORT, and `set_image()` does its own internal re-quantize against
its palette (doc §2) — which is why we hand it pure primaries in the first place. The `saturation`
argument feeds THAT re-quantize:

  SERVER (`epaper.render_for_epaper`)   gamma → dither to measured primaries → re-encode to PURE
  TRANSPORT, Inky only (`eink_client.py:146`)
                                        set_image(img, saturation=EINK_SATURATION)  [default 0.5]
                                        → inky re-quantizes our primaries to its ink mix

So saturation is not nudging a photograph — it is choosing which ink mix each of our six pure primaries
resolves to. Hence its outsized visible effect.

The consequence that matters for tuning: a DUMB BLITTER (Waveshare/ESP32, TRMNL BYOS) has no such
re-quantize — our primaries go straight to the inks. The same server render therefore resolves
differently on the two client classes, and anything tuned via EINK_SATURATION helps only the Inky. The
bench unit is Pimoroni; the enclosure spec targets the Waveshare 13.3" (SKU 29355). Tuning saturation
downstream risks dialling in something that doesn't transfer to the hardware the case is built for.

So `--saturation` here applies SERVER-side, before the dither: the candidate that generalises, where
the dither gets properly-saturated source and every client class sees the same intent. Treat inky's
re-quantize as a downstream stage to VERIFY against, not the place to encode the fix — render a
finalist at several server saturations and view it on both an Inky and a dumb blitter.

Contrast (1.12) and saturation (1.25) exist in `render_for_epaper` only for the NON-spectra6 palettes;
this tool can apply both to spectra6 so the bench can answer whether they belong there.

USAGE
-----
    # 1. What does the corpus actually look like, feature-wise?
    python -m tools.eink_calibrate stats --auto-corpus 24

    # 2. Contact sheets to narrow the grid (one refresh = six candidates)
    python -m tools.eink_calibrate sheet --auto-corpus 6 --gamma 1.0,1.3,1.6,1.9,2.2 --out bench/

    # 3. Full-panel renders of the finalists, judged at real size
    python -m tools.eink_calibrate full --images a.jpg b.jpg --gamma 1.6,1.9 --out bench/

    # 4. On the bench Pi, push one to the panel
    python -m tools.eink_calibrate push bench/sheet_01.png
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFont, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import epaper  # noqa: E402
from config import LIBRARY_DIR  # noqa: E402

PANEL_W, PANEL_H = 1600, 1200


# ---------------------------------------------------------------------------
# Candidate predictors. `wash_pct` is the incumbent; the rest are the challengers
# the bench session is meant to judge it against.
# ---------------------------------------------------------------------------
def predictors(img: Image.Image) -> dict:
    small = img.convert("RGB").resize((256, 256))
    r, g, b = small.split()
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    chroma = ImageChops.subtract(mx, mn)
    lum = small.convert("L")

    # Incumbent — mirrors epaper._adaptive_gamma exactly so the numbers are comparable.
    bright = lum.point(lambda v: 255 if v > 204 else 0)
    lowchroma = chroma.point(lambda v: 255 if v < 40 else 0)
    wash_pct = ImageChops.multiply(bright, lowchroma).histogram()[255] / (256 * 256) * 100.0

    lum_stat = ImageStat.Stat(lum)
    chroma_stat = ImageStat.Stat(chroma)

    # Edge density: cheap Sobel-ish proxy. Line art and engravings sit high here even when their
    # luminance histogram looks like a pale painting's — which is exactly where wash_pct misleads.
    edges = lum.filter(__import__("PIL.ImageFilter", fromlist=["FIND_EDGES"]).FIND_EDGES)
    # Count via the histogram, not `sum(... for v in getdata())`. Identical result — bins 49..255 ARE
    # the pixels above the threshold — but it stays in C instead of running 65536 Python iterations per
    # image. That difference is invisible on a laptop scanning 122 files and fatal on the appliance
    # scanning an installed 2857-work library, where `auto_corpus` calls this once per file.
    edge_pct = sum(edges.histogram()[49:]) / (256 * 256) * 100.0

    return {
        "wash_pct": round(wash_pct, 2),
        "mean_lum": round(lum_stat.mean[0], 1),
        "lum_stddev": round(lum_stat.stddev[0], 1),
        "mean_chroma": round(chroma_stat.mean[0], 1),
        "chroma_stddev": round(chroma_stat.stddev[0], 1),
        "edge_pct": round(edge_pct, 2),
        "current_gamma": round(epaper._adaptive_gamma(img.convert("RGB")), 3),
    }


# ---------------------------------------------------------------------------
# Rendering — production path, with the knobs forced.
# ---------------------------------------------------------------------------
def render_tile(path: Path, w: int, h: int, gamma: float,
                contrast: float = 1.0, saturation: float = 1.0) -> Image.Image:
    """One production-fidelity spectra6 render at explicit settings, returned as RGB."""
    fitted = epaper._fit_rgb(path, w, h, "cover", (0.5, 0.5), None)
    if abs(saturation - 1.0) > 1e-3:
        fitted = ImageEnhance.Color(fitted).enhance(saturation)
    if abs(contrast - 1.0) > 1e-3:
        fitted = ImageEnhance.Contrast(fitted).enhance(contrast)
    if gamma > 0:
        fitted = epaper._apply_gamma(fitted, gamma)
    q = fitted.quantize(
        palette=epaper._cached_palette_image("_spectra6_dither", epaper.SPECTRA6_DITHER_PALETTE),
        dither=Image.Dither.FLOYDSTEINBERG,
    )
    q.putpalette(epaper._flat_palette(epaper.SPECTRA6_OUTPUT_PALETTE))
    return q.convert("RGB")


def _font(size: int):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def contact_sheet(path: Path, settings: list[dict], cols: int = 3) -> Image.Image:
    """Tile one image at N settings, each cell labelled A/B/C… Pure black-on-white labels only —
    both are in the output palette, so the sheet stays panel-legal without a second quantize."""
    rows = (len(settings) + cols - 1) // cols
    bar, gut = 34, 8
    cell_w, cell_h = PANEL_W // cols, PANEL_H // rows
    cw, ch = cell_w - gut, cell_h - bar - gut
    sheet = Image.new("RGB", (PANEL_W, PANEL_H), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    font = _font(20)

    for i, s in enumerate(settings):
        cx = (i % cols) * cell_w + gut // 2
        cy = (i // cols) * cell_h + gut // 2
        sheet.paste(render_tile(path, cw, ch, **s), (cx, cy))
        # Pale art bleeds into its neighbours without a hard edge — on a panel at arm's length you
        # cannot tell where one candidate stops and the next starts. Black is in the output palette,
        # so the border costs nothing and the sheet stays panel-legal.
        draw.rectangle([cx, cy, cx + cw, cy + ch], outline=(0, 0, 0), width=2)
        label = f"{chr(65 + i)}   γ{s['gamma']}"
        if s.get("contrast", 1.0) != 1.0:
            label += f"  c{s['contrast']}"
        if s.get("saturation", 1.0) != 1.0:
            label += f"  s{s['saturation']}"
        draw.text((cx + 6, cy + ch + 7), label, fill=(0, 0, 0), font=font)
    return sheet


# ---------------------------------------------------------------------------
# Corpus selection — spread across the feature space, not random.
# ---------------------------------------------------------------------------
def _stratified(paths: list[Path], per_collection: int) -> list[Path]:
    """Evenly-spaced sample of at most `per_collection` files from each collection.

    Library filenames are `<collection>__<title>__<hash>.jpg`, so the prefix is the collection. Taking
    a slice per collection guarantees category coverage BY CONSTRUCTION rather than hoping the
    farthest-point pass stumbles onto it — and it is what makes the scan affordable: the candidate pool
    only has to COVER the feature space, not enumerate it.

    Evenly spaced rather than the first K: filenames sort by title, so `[:K]` would take everything
    alphabetically early, which correlates with subject matter far more than it looks.
    """
    groups: dict[str, list[Path]] = {}
    for p in paths:
        groups.setdefault(p.name.split("__", 1)[0], []).append(p)
    out: list[Path] = []
    for _, members in sorted(groups.items()):
        if len(members) <= per_collection:
            out.extend(members)
            continue
        step = len(members) / per_collection
        out.extend(members[int(i * step)] for i in range(per_collection))
    return sorted(out)


def auto_corpus(n: int, scan_cap: int | None = None) -> list[Path]:
    """Pick n images that MAXIMISE spread across the predictor space.

    `scan_cap` limits how many files per collection are SCANNED as candidates (None = all). Scanning
    is O(library) and decodes every master; labelling is O(n). Those are different budgets and
    conflating them is what made a 20-image session try to read a 2857-work library.

    A random sample under-represents exactly the cases that break the heuristic — the corpus that
    caused this problem was narrow, so sampling it the same way reproduces the blind spot. Greedy
    farthest-point selection on normalised features instead: each pick is the image most unlike
    everything already chosen.
    """
    paths = sorted(p for p in LIBRARY_DIR.glob("*.jpg"))
    if not paths:
        return []
    if scan_cap:
        paths = _stratified(paths, scan_cap)
    feats = []
    for p in paths:
        try:
            with Image.open(p) as im:
                # RANKING ONLY — decode at 1/8 scale via libjpeg's DCT scaling. `predictors` resizes to
                # 256x256 anyway, so full-resolution decode of a 4K+ master is pure waste: it dominates
                # the scan at ~700ms/image, which is 33min over an installed 2857-work library on a
                # laptop and multiples of that on the Pi. draft() makes it ~20x cheaper.
                #
                # The tiny feature differences this introduces are acceptable HERE and only here: this
                # pass decides which images to SHOW, a farthest-point ordering that is robust to small
                # perturbations. The features that reach the fit are recomputed at full fidelity from
                # the chosen files (tools.eink_bench.cmd_corpus), so no approximate value is ever
                # trained on.
                im.draft("RGB", (256, 256))
                f = predictors(im)
            feats.append((p, [f["wash_pct"] / 100, f["mean_lum"] / 255, f["mean_chroma"] / 255,
                              f["edge_pct"] / 100, f["lum_stddev"] / 128]))
        except Exception:
            continue
    if not feats:
        return []

    chosen = [max(feats, key=lambda t: t[1][0])]        # start at the washiest — the known-bad case
    while len(chosen) < min(n, len(feats)):
        best, best_d = None, -1.0
        for cand in feats:
            if any(cand[0] == c[0] for c in chosen):
                continue
            d = min(sum((a - b) ** 2 for a, b in zip(cand[1], c[1])) for c in chosen)
            if d > best_d:
                best, best_d = cand, d
        if best is None:
            break
        chosen.append(best)
    return [p for p, _ in chosen]


# ---------------------------------------------------------------------------
# Labelling + fit. The point of the bench session: turn Josh-at-the-panel into
# coefficients, so the shipped "auto" is derived from judgement instead of guessed.
# ---------------------------------------------------------------------------
# ONE definition of the feature vector, used by the fit AND emitted into the shipped function, so the
# two cannot drift. Scaled to roughly 0..1 — raw units (0..100 vs 0..255) are badly conditioned.
FEATURES = ("wash_pct", "mean_lum", "lum_stddev", "mean_chroma", "chroma_stddev", "edge_pct")
_SCALE = {"wash_pct": 100.0, "mean_lum": 255.0, "lum_stddev": 128.0,
          "mean_chroma": 255.0, "chroma_stddev": 128.0, "edge_pct": 100.0}


def feature_vector(p: dict) -> list[float]:
    return [p[f] / _SCALE[f] for f in FEATURES]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting. n<=7 here, so this is plenty and needs no numpy —
    which keeps the whole calibration path dependency-free."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError("singular system — need more, or more varied, labels")
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def ols(rows: list[dict]) -> tuple[list[float], float]:
    """Least squares with intercept. Returns (coeffs, r2); coeffs[0] is the intercept."""
    xs = [[1.0] + feature_vector(r["features"]) for r in rows]
    ys = [r["gamma"] for r in rows]
    k = len(xs[0])
    xtx = [[sum(x[i] * x[j] for x in xs) for j in range(k)] for i in range(k)]
    xty = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(k)]
    beta = _solve(xtx, xty)
    mean = sum(ys) / len(ys)
    ss_res = sum((y - sum(b * v for b, v in zip(beta, x))) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - mean) ** 2 for y in ys) or 1e-12
    return beta, 1.0 - ss_res / ss_tot


def _resolve(args) -> list[Path]:
    if args.images:
        return [Path(i) for i in args.images]
    return auto_corpus(args.auto_corpus)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("stats", "sheet", "full"):
        s = sub.add_parser(name)
        s.add_argument("--images", nargs="*", help="explicit image paths")
        s.add_argument("--auto-corpus", type=int, default=8, help="pick N max-spread images instead")
        if name != "stats":
            s.add_argument("--gamma", default="1.0,1.3,1.6,1.9,2.2")
            s.add_argument("--contrast", default="1.0")
            s.add_argument("--saturation", default="1.0")
            s.add_argument("--out", default="bench-eink")
            s.add_argument("--cols", type=int, default=3)

    lab = sub.add_parser("label", help="bench session: show a sheet, record which cell you picked")
    lab.add_argument("--images", nargs="*")
    lab.add_argument("--auto-corpus", type=int, default=20)
    lab.add_argument("--gamma", default="1.2,1.5,1.8,2.1,2.4,2.7")
    lab.add_argument("--contrast", default="1.0")
    lab.add_argument("--saturation", default="1.0")
    lab.add_argument("--out", default="bench-eink")
    lab.add_argument("--cols", type=int, default=3)
    lab.add_argument("--labels", default="bench-eink/labels.jsonl")
    lab.add_argument("--push", action="store_true", help="blit each sheet to the panel (run on the Pi)")

    fit = sub.add_parser("fit", help="derive the auto-setter from the labels")
    fit.add_argument("--labels", default="bench-eink/labels.jsonl")
    fit.add_argument("--holdout", type=int, default=0, help="reserve the last N labels to check against")

    p = sub.add_parser("push", help="run ON the bench Pi: send one PNG straight to the panel")
    p.add_argument("image")

    args = ap.parse_args()

    if args.cmd == "fit":
        rows = [json.loads(ln) for ln in Path(args.labels).read_text().splitlines() if ln.strip()]
        if len(rows) < len(FEATURES) + 2:
            sys.exit(f"only {len(rows)} labels — need at least {len(FEATURES) + 2} to fit {len(FEATURES)} features")
        train = rows[:-args.holdout] if args.holdout else rows
        beta, r2 = ols(train)

        print(f"# fitted on {len(train)} labels   R² = {r2:.3f}")
        print(f"# features: {', '.join(FEATURES)}")
        print("\n# --- paste into epaper.py, replacing _adaptive_gamma's body ---")
        print("_GAMMA_COEFFS = (")
        print(f"    {beta[0]:+.5f},   # intercept")
        for name, b in zip(FEATURES, beta[1:]):
            print(f"    {b:+.5f},   # {name} / {_SCALE[name]:g}")
        print(")")
        print("# gamma = intercept + sum(coeff * feature/scale), clamped to the labelled range.")
        print("# epaper.py also needs the feature computation — port `predictors()` from this tool")
        print("# (it already runs on a 256x256 downscale, same cost as today's _adaptive_gamma).")
        lo, hi = min(r["gamma"] for r in train), max(r["gamma"] for r in train)
        print(f"_GAMMA_RANGE = ({lo}, {hi})   # never extrapolate past what was actually judged")

        if args.holdout:
            print(f"\n# holdout — {args.holdout} pieces never used in the fit:")
            for r in rows[-args.holdout:]:
                pred = beta[0] + sum(b * v for b, v in zip(beta[1:], feature_vector(r["features"])))
                pred = max(lo, min(hi, pred))
                print(f"#   {Path(r['image']).name[:44]:46s} chose {r['gamma']:.2f}  "
                      f"predicted {pred:.2f}  ({pred - r['gamma']:+.2f})")
        return

    if args.cmd == "push":
        from inky.auto import auto  # noqa: PLC0415 — Pi-only dependency
        panel = auto()
        panel.set_image(Image.open(args.image).convert("RGB"))
        panel.show()
        print(f"pushed {args.image} to the panel")
        return

    images = _resolve(args)
    if not images:
        sys.exit("no images found — pass --images or check LIBRARY_DIR")

    if args.cmd == "stats":
        rows = []
        for p in images:
            with Image.open(p) as im:
                rows.append({"file": p.name, **predictors(im)})
        print(json.dumps(rows, indent=2))
        print(f"\n{len(rows)} images | current_gamma spread: "
              f"{min(r['current_gamma'] for r in rows)} .. {max(r['current_gamma'] for r in rows)}",
              file=sys.stderr)
        return

    grid = [
        {"gamma": g, "contrast": c, "saturation": s}
        for g in [float(x) for x in args.gamma.split(",")]
        for c in [float(x) for x in args.contrast.split(",")]
        for s in [float(x) for x in args.saturation.split(",")]
    ]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.cmd == "label":
        labels = Path(args.labels)
        labels.parent.mkdir(parents=True, exist_ok=True)
        done = {json.loads(ln)["image"] for ln in labels.read_text().splitlines() if ln.strip()} \
            if labels.exists() else set()
        letters = "".join(chr(65 + i) for i in range(len(grid)))
        print(f"{len(images)} images, {len(grid)} settings each. "
              f"Type a letter ({letters[0]}–{letters[-1]}), 's' to skip, 'q' to stop.\n"
              f"Already labelled: {len(done)} (those are skipped, so a session can resume).\n")

        for i, img in enumerate(images, 1):
            if str(img) in done:
                continue
            dest = out / f"label_{i:02d}_{Path(img).stem[:28]}.png"
            contact_sheet(Path(img), grid, cols=args.cols).save(dest)
            if args.push:
                from inky.auto import auto  # noqa: PLC0415
                panel = auto()
                panel.set_image(Image.open(dest).convert("RGB"))
                panel.show()
            print(f"[{i}/{len(images)}] {Path(img).name}\n    {dest}")

            while True:
                choice = input(f"    best of {letters}? ").strip().upper()
                if choice == "Q":
                    print("stopped — labels so far are saved")
                    return
                if choice == "S":
                    break
                idx = letters.find(choice)
                if idx >= 0:
                    with Image.open(img) as im:
                        feats = predictors(im)
                    with labels.open("a") as fh:
                        fh.write(json.dumps({"image": str(img), "choice": choice,
                                             **grid[idx], "features": feats}) + "\n")
                    print(f"    recorded {choice} → γ{grid[idx]['gamma']}")
                    break
                print(f"    not one of {letters} (or s/q)")
        print(f"\nlabels → {labels}\nnow:  python -m tools.eink_calibrate fit --labels {labels} --holdout 5")
        return

    for i, img in enumerate(images, 1):
        if args.cmd == "sheet":
            dest = out / f"sheet_{i:02d}_{Path(img).stem[:28]}.png"
            contact_sheet(Path(img), grid, cols=args.cols).save(dest)
            print(f"  {dest}   ({len(grid)} settings)")
        else:
            for s in grid:
                tag = f"g{s['gamma']}_c{s['contrast']}_s{s['saturation']}"
                dest = out / f"full_{i:02d}_{Path(img).stem[:24]}_{tag}.png"
                render_tile(Path(img), PANEL_W, PANEL_H, **s).save(dest)
                print(f"  {dest}")


if __name__ == "__main__":
    main()
