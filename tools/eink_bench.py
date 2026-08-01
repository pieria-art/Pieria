"""eink_bench — drive an e-ink calibration session in DISCRETE STEPS, not one interactive loop.

WHY THIS EXISTS ALONGSIDE `eink_calibrate label`
------------------------------------------------
`label` is one blocking `input()` loop at the Pi's console: it renders, pushes, and waits for a
keystroke, one image after another. That is the right shape when the person judging the panel is also
the person at the keyboard. It is the wrong shape when they are not — a remote operator driving the Pi
over SSH while someone stands at the panel calling letters out loud. There is no TTY to type into, and
a half-finished loop holds the session open.

So this splits the same work into four idempotent commands that each do one thing and exit:

    corpus            pick the max-spread corpus ONCE and freeze it to corpus.json
    show N            render sheet N and blit it to the panel
    record N LETTER   append that judgement to labels.jsonl
    status            what has been judged, what is left

FROZEN CORPUS IS THE POINT. `auto_corpus` re-runs greedy farthest-point selection over whatever is in
LIBRARY_DIR at that moment. On this appliance the library GROWS while packs install, so calling it per
step would silently re-order the corpus between `show N` and `record N` — labelling image A with the
features of image B, which is the one error a calibration harness must never make. Selection happens
exactly once, in `corpus`, and every later command reads the frozen file.

Everything else defers to tools.eink_calibrate (contact_sheet, predictors, FEATURES, the grid) so the
render path stays byte-identical to the one `fit` was verified against. Run under sudo: config.py
reads a root-owned .env, and the panel needs SPI.

    sudo python3 -m tools.eink_bench corpus --n 20
    sudo python3 -m tools.eink_bench show 1
    sudo python3 -m tools.eink_bench record 1 C
    python3 -m tools.eink_calibrate fit --labels bench-eink/labels.jsonl --holdout 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageEnhance

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import eink_calibrate as ec  # noqa: E402

OUT = Path("bench-eink")
CORPUS = OUT / "corpus.json"
LABELS = OUT / "labels.jsonl"

#: Same grid the laptop dry-run was validated against: it brackets both extremes of the corpus
#: (pale line art wants ~2.1-2.4, dark oils want ~1.2) with the optimum interior, not clipped.
GRID_GAMMAS = (1.2, 1.5, 1.8, 2.1, 2.4, 2.7)
COLS = 3


def _grid() -> list[dict]:
    return [{"gamma": g, "contrast": 1.0, "saturation": 1.0} for g in GRID_GAMMAS]


def _letters() -> str:
    return "".join(chr(65 + i) for i in range(len(GRID_GAMMAS)))


def _load_corpus() -> list[dict]:
    if not CORPUS.exists():
        sys.exit(f"no {CORPUS} — run `corpus` first")
    return json.loads(CORPUS.read_text())


def _labelled() -> dict:
    if not LABELS.exists():
        return {}
    rows = [json.loads(ln) for ln in LABELS.read_text().splitlines() if ln.strip()]
    return {r["image"]: r for r in rows}


def cmd_corpus(args) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if CORPUS.exists() and not args.force:
        sys.exit(f"{CORPUS} already exists — refusing to reshuffle a session in progress "
                 f"(pass --force to start over; existing labels would no longer match)")
    paths = ec.auto_corpus(args.n, scan_cap=(args.scan_cap or None))
    if not paths:
        sys.exit("no images found in LIBRARY_DIR")
    rows = []
    for i, p in enumerate(paths, 1):
        with Image.open(p) as im:
            feats = ec.predictors(im)
        rows.append({"n": i, "image": str(p), "features": feats})
    CORPUS.write_text(json.dumps(rows, indent=2))
    print(f"froze {len(rows)} images -> {CORPUS}\n")
    print(f"{'n':>3} {'wash':>6} {'lum':>6} {'chroma':>7} {'edge':>6} {'now':>5}  file")
    for r in rows:
        f = r["features"]
        print(f"{r['n']:3d} {f['wash_pct']:6.1f} {f['mean_lum']:6.1f} {f['mean_chroma']:7.1f} "
              f"{f['edge_pct']:6.1f} {f['current_gamma']:5.2f}  {Path(r['image']).name[:52]}")


def _norm(f: dict) -> list:
    """The normalised vector auto_corpus ranks on. Kept identical so `extend` continues the SAME
    farthest-point walk rather than starting a second, unrelated one."""
    return [f["wash_pct"] / 100, f["mean_lum"] / 255, f["mean_chroma"] / 255,
            f["edge_pct"] / 100, f["lum_stddev"] / 128]


def cmd_extend(args) -> None:
    """Add N more images to a frozen corpus, seeded with everything already chosen.

    A second independent `corpus` run would re-pick from scratch and hand back images already judged —
    and worse, would pick its new spread without knowing where the first batch already sampled. Seeding
    the greedy selection with the existing feature vectors makes batch two cover what batch one MISSED,
    which is the entire reason to run a second batch: the first fit's error (0.266) sat above the
    judge's own repeatability (0.180), so the gap is uncaptured structure, not noise.
    """
    rows = _load_corpus()
    have_paths = {r["image"] for r in rows}
    chosen = [_norm(r["features"]) for r in rows]

    paths = sorted(p for p in ec.LIBRARY_DIR.glob("*.jpg"))
    if args.scan_cap:
        paths = ec._stratified(paths, args.scan_cap)
    paths = [p for p in paths if str(p) not in have_paths]
    print(f"scanning {len(paths)} candidates (excluding {len(have_paths)} already in the corpus)")

    feats = []
    for p in paths:
        try:
            with Image.open(p) as im:
                im.draft("RGB", (256, 256))
                f = ec.predictors(im)
            feats.append((p, _norm(f)))
        except Exception:
            continue

    picked = []
    while len(picked) < args.n and feats:
        best, best_d = None, -1.0
        for cand in feats:
            if any(cand[0] == c[0] for c in picked):
                continue
            d = min(sum((a - b) ** 2 for a, b in zip(cand[1], c)) for c in chosen)
            if d > best_d:
                best, best_d = cand, d
        if best is None:
            break
        picked.append(best)
        chosen.append(best[1])

    start = max(r["n"] for r in rows)
    new = []
    for i, (p, _) in enumerate(picked, start + 1):
        with Image.open(p) as im:
            new.append({"n": i, "image": str(p), "features": ec.predictors(im)})
    CORPUS.write_text(json.dumps(rows + new, indent=2))
    print(f"appended {len(new)} images -> corpus now {len(rows) + len(new)}\n")
    print(f"{'n':>3} {'wash':>6} {'lum':>6} {'chroma':>7} {'edge':>6}  file")
    for r in new:
        f = r["features"]
        print(f"{r['n']:3d} {f['wash_pct']:6.1f} {f['mean_lum']:6.1f} {f['mean_chroma']:7.1f} "
              f"{f['edge_pct']:6.1f}  {Path(r['image']).name[:54]}")


def _db_crop_and_focal(filename: str, w: int, h: int):
    """The AUTHORED crop the production endpoint would use, plus the work's focal point.

    `render_tile` (the contact-sheet path) passes crop_box=None and focal-cover-crops instead. That is
    not what ships: `routers/display.py` calls `pick_crop_for_aspect(art.aspect_crops, w, h)` (ADR-055).
    The difference is not cosmetic — on a full-sheet Audubon scan the authored 4:3 box is
    [0, 0.29, 1, 0.81], tight to the plate and its caption, while the focal cover crop of a near-square
    tile is mostly blank margin. An hour of the first session was spent judging paper because of it.
    """
    import sqlite3  # noqa: PLC0415 — only needed on the appliance
    db = Path("data/artwork.db")
    if not db.exists():
        return None, (0.5, 0.5)
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "select aspect_crops_json, focal_x, focal_y from artworks where filename = ?",
            (filename,)).fetchone()
    finally:
        con.close()
    if not row:
        return None, (0.5, 0.5)
    crops_json, fx, fy = row
    focal = (fx if fx is not None else 0.5, fy if fy is not None else 0.5)
    try:
        crops = json.loads(crops_json) if crops_json else None
    except (TypeError, ValueError):
        crops = None
    return ec.epaper.pick_crop_for_aspect(crops, w, h), focal


def cmd_full(args) -> None:
    """Render ONE candidate at full panel resolution, with the authored crop, and blit it.

    This supersedes the contact sheet for judgement. A 3x2 sheet gives each candidate 1/6 of the panel,
    which does two things that invalidate the result: it under-resolves fine detail, and — worse — it
    breaks the DITHER. Floyd-Steinberg approximates an out-of-gamut colour by scattering pure primaries
    that are meant to fuse at the panel's native resolution and viewing distance; at 1/6 area those dots
    are ~3x larger relative to the image and read as garish speckle instead. The 2026-08-01 session
    nearly concluded that e-ink could not reproduce art at all, on the strength of that artifact alone.
    Contact sheets are for BRACKETING a range cheaply; every judgement that decides anything is full-panel.
    """
    rows = _load_corpus()
    row = next((r for r in rows if r["n"] == args.n), None)
    if row is None:
        sys.exit(f"no corpus entry {args.n} (have 1..{max(r['n'] for r in rows)})")
    img = Path(row["image"])
    w, h = args.width, args.height
    crop, focal = _db_crop_and_focal(img.name, w, h)

    fitted = ec.epaper._fit_rgb(img, w, h, "cover", focal, crop)
    if abs(args.saturation - 1.0) > 1e-3:
        fitted = ImageEnhance.Color(fitted).enhance(args.saturation)
    if abs(args.contrast - 1.0) > 1e-3:
        fitted = ImageEnhance.Contrast(fitted).enhance(args.contrast)
    if args.gamma > 0:
        fitted = ec.epaper._apply_gamma(fitted, args.gamma)
    q = fitted.quantize(
        palette=ec.epaper._cached_palette_image("_spectra6_dither", ec.epaper.SPECTRA6_DITHER_PALETTE),
        dither=Image.Dither.FLOYDSTEINBERG)
    q.putpalette(ec.epaper._flat_palette(ec.epaper.SPECTRA6_OUTPUT_PALETTE))
    out = q.convert("RGB")

    OUT.mkdir(parents=True, exist_ok=True)
    # Dimensions belong in the name: the same image at 1600x1200 and 1200x1600 resolves to DIFFERENT
    # authored crops (4:3 vs 3:4), so they are different renders, not the same one twice.
    dest = OUT / f"full_{args.n:02d}_{w}x{h}_g{args.gamma}_s{args.saturation}_c{args.contrast}.png"
    out.save(dest)
    print(f"[{args.n}] {img.name}")
    print(f"  {w}x{h}  gamma {args.gamma}  saturation {args.saturation}  contrast {args.contrast}")
    print(f"  crop {crop if crop else 'NONE (focal cover)'}  focal {focal}")
    print(f"  {dest}")
    if args.no_push:
        return
    from inky.auto import auto  # noqa: PLC0415
    panel = auto()
    panel.set_image(out)
    panel.show()
    print("  pushed to panel")


def cmd_show(args) -> None:
    rows = _load_corpus()
    row = next((r for r in rows if r["n"] == args.n), None)
    if row is None:
        sys.exit(f"no corpus entry {args.n} (have 1..{len(rows)})")
    img = Path(row["image"])
    dest = OUT / f"sheet_{args.n:02d}.png"
    ec.contact_sheet(img, _grid(), cols=COLS).save(dest)
    print(f"[{args.n}/{len(rows)}] {img.name}")
    print(f"  sheet: {dest}")
    print(f"  cells: {'  '.join(f'{chr(65+i)}=g{g}' for i, g in enumerate(GRID_GAMMAS))}")
    if args.no_push:
        return
    from inky.auto import auto  # noqa: PLC0415 — Pi-only dependency
    panel = auto()
    panel.set_image(Image.open(dest).convert("RGB"))
    panel.show()
    print("  pushed to panel")


def cmd_record(args) -> None:
    rows = _load_corpus()
    row = next((r for r in rows if r["n"] == args.n), None)
    if row is None:
        sys.exit(f"no corpus entry {args.n} (have 1..{len(rows)})")
    letters = _letters()
    # A judge torn between two adjacent cells carries REAL information, and forcing a pick throws it
    # away: the grid steps by 0.3, so every letter-label already carries +/-0.15 of quantisation noise,
    # and a coin flip between D and E doubles that for no reason. `fit` does ordinary least squares on
    # a CONTINUOUS gamma, so an off-grid midpoint is not merely allowed — it is a better observation
    # than either neighbour. Clamped to the judged grid so a typo can't plant a value nobody ever saw.
    if args.gamma is not None:
        # Bounds are the widest gamma any sheet may present, NOT the standard grid. The first session
        # hit the standard ceiling immediately: the engraved world map picked F (2.7), an EXTENDED
        # 2.4-3.9 grid was shown to test it, and 3.0 won — so 2.7 had been a censored observation, not
        # an optimum. Clamping to GRID_GAMMAS would refuse the very value the judge actually saw. The
        # guard that matters is only that nobody types a gamma no sheet could have displayed.
        lo, hi = 1.0, 4.0
        if not lo <= args.gamma <= hi:
            sys.exit(f"--gamma {args.gamma} is outside the plausible range {lo}..{hi}")
        setting = {"gamma": round(args.gamma, 3), "contrast": 1.0, "saturation": 1.0}
        near = min(range(len(GRID_GAMMAS)), key=lambda i: abs(GRID_GAMMAS[i] - args.gamma))
        choice = f"~{letters[near]}"
    else:
        choice = args.letter.strip().upper()
        idx = letters.find(choice)
        if idx < 0:
            sys.exit(f"{choice!r} is not one of {letters} (or use --gamma for a between-cells call)")
        setting = _grid()[idx]
    already = _labelled().get(row["image"])
    if already and not args.force:
        sys.exit(f"image {args.n} already labelled {already['choice']} (γ{already['gamma']}) "
                 f"— pass --force to re-judge it")
    if already:
        # REPLACE, never append. `fit` reads every line of the JSONL, so a second row for the same
        # image would train on BOTH the superseded judgement and the new one — silently double-weighting
        # that image and averaging in an answer the judge has explicitly retracted. Re-judging happened
        # on the very first session (a "defect" turned out to be the artwork's real paper tone), so this
        # path is normal, not exceptional.
        kept = [ln for ln in LABELS.read_text().splitlines()
                if ln.strip() and json.loads(ln)["image"] != row["image"]]
        LABELS.write_text("".join(ln + "\n" for ln in kept))
        print(f"  superseded previous label {already['choice']} (γ{already['gamma']})")
    OUT.mkdir(parents=True, exist_ok=True)
    rec = {"image": row["image"], "choice": choice, **setting, "features": row["features"]}
    # A bare letter records WHICH cell won but not WHY, and the why is often the more useful artifact:
    # the first real session immediately turned up a conflict a single gamma cannot resolve (detail
    # improving with gamma while the near-white background picked up a yellow dither cast). That is
    # evidence for a change to the QUANTISE step, not the tone curve — and it would have been lost.
    # `fit` ignores this field; it exists for the human reading the labels afterwards.
    if args.note:
        rec["note"] = args.note
    with LABELS.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(f"recorded {args.n} -> {choice} (γ{setting['gamma']})   [{len(_labelled())} labelled]")


def cmd_status(args) -> None:
    rows = _load_corpus()
    done = _labelled()
    n_done = sum(1 for r in rows if r["image"] in done)
    print(f"{n_done}/{len(rows)} labelled   (need >= {len(ec.FEATURES) + 2} to fit)")
    for r in rows:
        d = done.get(r["image"])
        mark = f"{d['choice']} γ{d['gamma']}" if d else "  --  "
        print(f"  {r['n']:3d}  {mark:9s}  {Path(r['image']).name[:56]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("corpus", help="pick + freeze the max-spread corpus")
    c.add_argument("--n", type=int, default=20)
    c.add_argument("--force", action="store_true", help="reshuffle even if a corpus exists")
    c.add_argument("--scan-cap", type=int, default=12,
                   help="max files per collection to SCAN as candidates (0 = whole library). "
                        "Scanning is O(library) and decodes every master; labelling is O(n).")

    s = sub.add_parser("show", help="render sheet N and push it to the panel")
    s.add_argument("n", type=int)
    s.add_argument("--no-push", action="store_true", help="render only (no panel, no SPI)")

    r = sub.add_parser("record", help="append the judgement for sheet N")
    r.add_argument("n", type=int)
    r.add_argument("letter", nargs="?", default="")
    r.add_argument("--gamma", type=float, default=None,
                   help="record an off-grid gamma (e.g. 2.25 when torn between D and E)")
    r.add_argument("--force", action="store_true")
    r.add_argument("--note", default="", help="why this cell won — free text, kept with the label")

    fu = sub.add_parser("full", help="render ONE candidate at full panel with the authored crop")
    fu.add_argument("n", type=int)
    fu.add_argument("--gamma", type=float, default=1.8)
    fu.add_argument("--saturation", type=float, default=1.0)
    fu.add_argument("--contrast", type=float, default=1.0)
    fu.add_argument("--width", type=int, default=1600)
    fu.add_argument("--height", type=int, default=1200)
    fu.add_argument("--no-push", action="store_true")

    e = sub.add_parser("extend", help="append N more images, seeded with the existing corpus")
    e.add_argument("--n", type=int, default=30)
    e.add_argument("--scan-cap", type=int, default=12)

    sub.add_parser("status", help="what is judged and what is left")

    args = ap.parse_args()
    {"corpus": cmd_corpus, "show": cmd_show, "record": cmd_record,
     "status": cmd_status, "extend": cmd_extend, "full": cmd_full}[args.cmd](args)


if __name__ == "__main__":
    main()
