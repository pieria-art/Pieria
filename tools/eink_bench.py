"""eink_bench — drive an e-ink calibration session in DISCRETE STEPS, not one interactive loop.

WHY THIS EXISTS ALONGSIDE `eink_calibrate label`
------------------------------------------------
`label` is one blocking `input()` loop at the Pi's console: it renders, pushes, and waits for a
keystroke, one image after another. That is the right shape when the person judging the panel is also
the person at the keyboard. It is the wrong shape when they are not — a remote operator driving the Pi
over SSH while someone stands at the panel calling letters out loud. There is no TTY to type into, and
a half-finished loop holds the session open.

So this splits the same work into idempotent commands that each do one thing and exit:

    corpus            pick the max-spread corpus ONCE and freeze it to corpus.json
    show N            render sheet N and blit it to the panel        (BRACKETING only — ADR-084)
    record N LETTER   append that judgement to labels.jsonl
    status            what has been judged, what is left

    reference         regenerate the laptop ground truth, through the render's own crop path
    full N            render ONE candidate at full panel, authored crop, and blit it   (DECIDES)
    classify N CLASS  pre-register a work's MATERIAL class, before it is judged
    full-record N V   record a full-panel A/B verdict
    full-status       campaign progress, by verdict and by material class

`show` vs `full` is the ADR-084 split and it is not a preference: a 3x2 contact sheet gives each
candidate 1/6 of the panel, which breaks the dither (primaries meant to FUSE at native resolution read
as speckle) and used the wrong crop. Sixty judgements were made against that artifact and had to be
demoted to a prior. Sheets may cheaply bracket a range; anything that decides something is `full`.

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


def _db_crop_key(filename: str, key: str):
    """A specific authored box by key, bypassing nearest-aspect selection."""
    import sqlite3  # noqa: PLC0415
    db = Path("data/artwork.db")
    if not db.exists():
        return None
    con = sqlite3.connect(str(db))
    try:
        row = con.execute("select aspect_crops_json from artworks where filename = ?", (filename,)).fetchone()
    finally:
        con.close()
    try:
        return tuple(json.loads(row[0])[key]) if row and row[0] else None
    except (TypeError, ValueError, KeyError):
        return None


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
    if args.box:
        # An explicit normalised box, for AUTHORING a better grab than the stored preset. The stored
        # 4:3 for a full-sheet scan preserves the whole plate width and accepts side margins; sometimes
        # the right answer is to crop INTO the work and fill the frame with the subject.
        crop = tuple(float(v) for v in args.box.split(","))
        if len(crop) != 4:
            sys.exit("--box needs x0,y0,x1,y1 normalised 0..1")
        if args.save_box:
            # Persist it so `reference` frames the ground truth the SAME way. An authored box that
            # lives only in one shell command is how the panel and the laptop end up showing two
            # different compositions of the same work.
            boxes = _load_json(BOXES, {})
            boxes[str(args.n)] = list(crop)
            BOXES.write_text(json.dumps(boxes, indent=1, sort_keys=True))
            print(f"  saved authored box for {args.n} -> {BOXES} (re-run `reference` to match)")
    elif _authored_box(args.n) is not None and args.crop_key == "auto":
        crop = _authored_box(args.n)
    elif args.crop_key == "none":
        crop = None
    elif args.crop_key != "auto":
        # Containing a box that is ALREADY the target aspect is a no-op — the whole point of `contain`
        # for portrait art on a landscape panel is to letterbox a PORTRAIT framing (3:4) into it.
        crop = _db_crop_key(img.name, args.crop_key)

    fitted = ec.epaper._fit_rgb(img, w, h, args.fit, focal, crop)
    if args.white_point > 0:
        # WHITE-POINT COMPRESSION, not gamma. The palette's lightest ink is white at luminance ~163,
        # so every input above that has no ink to be built from and renders as flat white: measured,
        # the top 38% of the input range collapses to a single output value. Gamma cannot fix it —
        # it preserves endpoints, so 255 still maps to 255 and still clips. Scaling does.
        fitted = fitted.point([min(255, int(round(i * args.white_point))) for i in range(256)] * 3)
    if args.chroma_floor_max is not None:
        # HUE-CONDITIONED floor (ADR-088 correction, 2026-08-28). The scalar floor below cannot serve
        # two works at once — June's skin must lose its colour while Sunflowers' wall must keep its —
        # but those populations separate at 0.999 accuracy on HUE alone, so the floor is keyed on how
        # well any ink can serve the pixel's hue instead of being one number per image.
        fitted = ec.epaper.apply_chroma_curve(fitted, args.chroma_gamma, args.chroma_floor_max,
                                              args.chroma_hue_e0,
                                              gap_normalised=args.chroma_gap_normalised,
                                              floor_min=args.chroma_floor_min)
    elif abs(args.chroma_gamma - 1.0) > 1e-3:
        # A CURVE on chroma, not a multiplier. `ImageEnhance.Color(k)` scales every pixel's saturation
        # by the same k, which cannot serve one frame containing both a saturated gown and desaturated
        # skin: halving it fixed the skin and killed the gown (Flaming June, 2026-08-01).
        #
        # The dither's actual failure is gamut compression toward the hull — LOW-chroma tones acquire
        # false colour (tan reads golden, skin reads orange) because they must be built from vivid
        # primaries, while genuinely saturated colour is already served. So attenuate by how saturated
        # a pixel ALREADY is: s' = s**k. At k=2, s=0.2 -> x0.20 but s=0.8 -> x0.80.
        hue, sat, val = fitted.convert("HSV").split()   # NOT h/v — `h` is the panel height
        # s' = max(s**k, s*floor) — a curve with a floor.
        #
        # Pure s**k fails at both ends of one frame: at k=2 it takes very faint colour (s=0.2) down to
        # x0.20, erasing the finch's butterflies, while at k=1.5 it leaves MID chroma (s=0.5-0.6, the
        # tan reeds) at x0.55-0.70 and the golden cast returns. The floor keeps faint colour alive; the
        # curve still crushes the mid-range and spares genuinely saturated content (Flaming June's gown).
        lut = [min(255, int(round(255.0 * max((i / 255.0) ** args.chroma_gamma,
                                              (i / 255.0) * args.chroma_floor)))) for i in range(256)]
        fitted = Image.merge("HSV", (hue, sat.point(lut), val)).convert("RGB")
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
    # The chroma recipe belongs in the name too: a hue-conditioned render and a scalar-floor render at
    # the same gamma are different candidates, and a judgement filed against the wrong one is the
    # error this harness exists to prevent.
    wp_tag = f"_wp{args.white_point}" if args.white_point > 0 else ""
    if args.chroma_floor_max is None:
        chroma_tag = f"_fl{args.chroma_floor}"
    elif args.chroma_gap_normalised:
        chroma_tag = f"_hf{args.chroma_floor_max}gapmin{args.chroma_floor_min}"
    else:
        chroma_tag = f"_hf{args.chroma_floor_max}e{args.chroma_hue_e0}"
    dest = (OUT / f"full_{args.n:02d}_{w}x{h}_{args.fit}_g{args.gamma}{wp_tag}_k{args.chroma_gamma}"
                  f"{chroma_tag}_s{args.saturation}_c{args.contrast}.png")
    out.save(dest)
    print(f"[{args.n}] {img.name}")
    if args.chroma_floor_max is None:
        chroma_desc = f"scalar floor {args.chroma_floor}"
    elif args.chroma_gap_normalised:
        chroma_desc = (f"hue-conditioned GAP-NORMALISED floor_max {args.chroma_floor_max} "
                       f"floor_min {args.chroma_floor_min}")
    else:
        chroma_desc = f"hue-conditioned floor_max {args.chroma_floor_max} e0 {args.chroma_hue_e0}"
    print(f"  {w}x{h}  gamma {args.gamma}  chroma_gamma {args.chroma_gamma}  "
          f"saturation {args.saturation}  contrast {args.contrast}")
    print(f"  chroma: {chroma_desc}")
    if args.white_point > 0:
        print(f"  white-point: {args.white_point} (255 -> {round(255*args.white_point)}; "
              f"white ink is 163)")
    print(f"  fit {args.fit}  crop {crop if crop else 'NONE (focal cover)'}  focal {focal}")
    print(f"  {dest}")
    # Tell the laptop what the panel is about to show, so the judge's ground truth follows the panel
    # rather than being stepped by hand. Written even with --no-push: a render that was not pushed is
    # still the last thing decided about, and a stale pointer is worse than a slightly early one.
    if REF.exists():
        (REF / "current.json").write_text(json.dumps({"n": args.n, "dest": dest.name}))
    if args.no_push:
        return
    from inky.auto import auto  # noqa: PLC0415
    panel = auto()
    pw, ph = panel.resolution
    shown = out
    if (out.width, out.height) != (pw, ph):
        # Portrait composition on a physically landscape buffer — exactly what eink_client does when
        # EINK_ORIENTATION=portrait: the server frames at h x w, the client rotates it back onto the
        # panel's native buffer. Turn the panel 90 degrees to view.
        shown = out.rotate(90, expand=True)
        print(f"  rotated {out.width}x{out.height} -> {shown.width}x{shown.height} for the panel "
              f"(turn the panel 90 degrees)")
    panel.set_image(shown)
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


# --- The judgement rig: panel + laptop reference, same work, same framing ------------------------
#
# "Faithful" is undefined without a ground truth. The 2026-08-01 session judged sixty renders with no
# reference at all — picking the least-bad of six variants of the same wrong thing (ADR-084). The
# reference viewer fixed that, but two gaps remained until now:
#
#   1. the refs were the UNCROPPED sources, so a subject-filling panel render was compared against a
#      full-sheet reference — two different compositions, which re-imports the ADR-084 error a level up;
#   2. nothing advanced the laptop when the panel changed, so page and panel could silently drift
#      apart mid-campaign — the image-vs-label misalignment the frozen corpus exists to prevent.
#
# Both close by generating and SERVING the references from the Pi: `reference` renders each work
# through the same crop path as `full` (minus the quantise), `full` writes current.json, and the page
# follows it. The laptop is then just a browser and there is no cross-machine copy to fall out of sync.
#
#     sudo python3 -m tools.eink_bench reference          # regenerate, through the render's crop path
#     python3 -m http.server 8090 --directory bench-eink/reference
#     # laptop browser -> http://<pi>:8090/   (press l to toggle follow)

FULLPANEL = OUT / "fullpanel.jsonl"
CLASSES = OUT / "classes.json"
BOXES = OUT / "boxes.json"
REF = OUT / "reference"

#: MATERIAL classes, not collections. The 2026-08-02 conflict was semantic — June's skin must go
#: neutral while Sunflowers' wall is genuinely yellow — and material is what encodes that. Assigned
#: from the WORK before judging: post-hoc slicing of a finished dataset is fishing.
MATERIAL_CLASSES = (
    "aged-paper-plate",   # engraved/lithographic plates and prints on visible paper stock
    "oil-painting",       # canvas, scanned edge to edge
    "flat-ink-print",     # woodblock/ukiyo-e/poster — flat areas, hard boundaries
    "mono-photograph",    # greyscale photographic prints
    "colour-photograph",  # photographs carrying real colour — incl. photochroms and toned albumen
    "neutral-sculpture",  # 3-D objects, near-neutral, lit
    "dark-field",         # astro/night — mostly black ground
)
# ⚠️ CLASS IS THE MATERIAL, NOT A FEATURE THRESHOLD. `the-shipwreck` (n57) measures mean_chroma 0.0
# yet is a monochrome ENGRAVING on paper — an aged-paper-plate whose render problems are a plate's,
# not a photograph's. Assign by looking at the work; the features are inputs to the model, not the
# label. `colour-photograph` was added 2026-08-28 only after looking: the first list had nowhere to
# put the Hubble frame, a photochrom, or a sepia albumen print, and they would have been forced into
# a class they do not behave like.

VERDICTS = ("new", "incumbent", "tie", "both-bad")


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _authored_box(n: int):
    """A hand-authored crop override for work n, shared by BOTH the render and the reference.

    ADR-087 changed what a crop is for on a landscape panel — fill the frame with the subject rather
    than preserve the composition — and those boxes are authored one at a time on the panel before
    the catalog-wide re-derivation runs. Keeping them in one file that `full` and `reference` both
    read is what stops the panel showing a subject grab while the laptop shows the whole sheet.
    """
    box = _load_json(BOXES, {}).get(str(n))
    return tuple(float(v) for v in box) if box and len(box) == 4 else None


def _title_of(image_path: str) -> tuple:
    """('collection', 'title') from a _Library filename `collection__title__hash.jpg`."""
    stem = Path(image_path).stem
    parts = stem.split("__")
    coll = parts[0] if parts else ""
    title = parts[1].replace("-", " ") if len(parts) > 1 else stem
    return coll, title


def cmd_reference(args) -> None:
    """Regenerate the laptop reference set THROUGH the render's crop path (no quantise).

    Must be re-run whenever the framing changes — a new authored box, or the ADR-087 re-derivation —
    otherwise the ground truth silently stops being the thing on the panel.
    """
    rows = _load_corpus()
    REF.mkdir(parents=True, exist_ok=True)
    w, h = args.width, args.height
    meta = []
    for row in rows:
        n, img = row["n"], Path(row["image"])
        if not img.exists():
            print(f"  [{n:2d}] MISSING {img}")
            continue
        crop, focal = _db_crop_and_focal(img.name, w, h)
        box = _authored_box(n)
        if box:
            crop = box
        framed = ec.epaper._fit_rgb(img, w, h, args.fit, focal, crop)
        dest = REF / f"ref_{n:02d}.jpg"
        framed.save(dest, "JPEG", quality=92)
        coll, title = _title_of(row["image"])
        meta.append({"n": n, "file": dest.name, "collection": coll, "title": title,
                     "features": row.get("features", {}),
                     "crop": list(crop) if crop else None,
                     "authored_box": bool(box)})
        print(f"  [{n:2d}] {dest.name}  crop {'AUTHORED ' if box else ''}{crop if crop else 'none (focal cover)'}")
    (REF / "meta.json").write_text(json.dumps(meta, indent=1))
    (REF / "index.html").write_text(_reference_html(meta, w, h))
    print(f"\n{len(meta)} references at {w}x{h} ({args.fit}) -> {REF}/")
    print(f"serve:  python3 -m http.server {args.port} --directory {REF}")
    print(f"open :  http://<this-pi>:{args.port}/     (l = follow the panel, i = features, f = fullscreen)")


def _reference_html(meta: list, w: int, h: int) -> str:
    """The viewer. Neutral surround is not decoration — see the comment in the emitted CSS."""
    return _REF_HTML_TEMPLATE.replace("__EMBEDDED__", json.dumps(meta)).replace("__PANEL__", f"{w}x{h}")


def cmd_full_record(args) -> None:
    """Record a full-panel A/B verdict. Refuses a work with no pre-registered class."""
    rows = _load_corpus()
    row = next((r for r in rows if r["n"] == args.n), None)
    if row is None:
        sys.exit(f"no corpus entry {args.n}")
    if args.verdict not in VERDICTS:
        sys.exit(f"verdict must be one of {', '.join(VERDICTS)}")
    classes = _load_json(CLASSES, {})
    cls = classes.get(str(args.n))
    if not cls and not args.force:
        sys.exit(f"work {args.n} has no pre-registered class — run `classify {args.n} <class>` first "
                 f"(or --force). Assigning a class AFTER seeing the result is fishing, not a hypothesis.")
    done = {}
    if FULLPANEL.exists():
        done = {json.loads(ln)["n"]: json.loads(ln)
                for ln in FULLPANEL.read_text().splitlines() if ln.strip()}
    if args.n in done and not args.force:
        sys.exit(f"work {args.n} already recorded ({done[args.n]['verdict']}) — pass --force to replace")
    rec = {"n": args.n, "image": row["image"], "class": cls, "verdict": args.verdict,
           "preference": args.preference, "candidate": args.candidate, "note": args.note,
           "features": row.get("features", {})}
    lines = [json.dumps(v) for k, v in sorted(done.items()) if k != args.n] + [json.dumps(rec)]
    FULLPANEL.write_text("\n".join(lines) + "\n")
    print(f"[{args.n}] {args.verdict}   class={cls}   {row['image'].split('__')[1] if '__' in row['image'] else ''}")
    print(f"  {len(lines)}/{len(rows)} recorded")


def cmd_full_status(args) -> None:
    rows = _load_corpus()
    classes = _load_json(CLASSES, {})
    done = {}
    if FULLPANEL.exists():
        done = {json.loads(ln)["n"]: json.loads(ln)
                for ln in FULLPANEL.read_text().splitlines() if ln.strip()}
    print(f"{len(done)}/{len(rows)} judged at full panel   "
          f"{sum(1 for r in rows if classes.get(str(r['n'])))}/{len(rows)} classified")
    tally, by_class = {}, {}
    for r in rows:
        n = r["n"]
        cls = classes.get(str(n), "-")
        d = done.get(n)
        if d:
            tally[d["verdict"]] = tally.get(d["verdict"], 0) + 1
            by_class.setdefault(cls, {}).setdefault(d["verdict"], 0)
            by_class[cls][d["verdict"]] += 1
        if args.verbose or not d:
            coll, title = _title_of(r["image"])
            print(f"  {n:3d}  {(d['verdict'] if d else '--'):10s} {cls:18s} {title[:44]}")
    if tally:
        print("\noverall: " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    split = [d for d in done.values() if d.get("preference") and d["preference"] != d["verdict"]]
    if split:
        print(f"fidelity/preference SPLIT on {len(split)}/{len(done)} judged: "
              + ", ".join(f"{d['n']}({d['class']})" for d in split))
        print("  ^ the render that is closer to the reference is NOT the one the judge would hang.")
    for cls, t in sorted(by_class.items()):
        total = sum(t.values())
        print(f"  {cls:18s} n={total:3d}  " + "  ".join(f"{k}={v}" for k, v in sorted(t.items())))
        if total < 5:
            print("      ^ thin cell — too few to claim a within-class model from")


def cmd_classify(args) -> None:
    rows = _load_corpus()
    classes = _load_json(CLASSES, {})
    if args.n is None:
        unset = [r["n"] for r in rows if not classes.get(str(r["n"]))]
        print("classes: " + ", ".join(MATERIAL_CLASSES))
        counts = {}
        for r in rows:
            counts[classes.get(str(r["n"]), "-")] = counts.get(classes.get(str(r["n"]), "-"), 0) + 1
        for k, v in sorted(counts.items()):
            print(f"  {k:18s} {v:3d}")
        if unset:
            print(f"\nunclassified ({len(unset)}): {' '.join(str(u) for u in unset)}")
        return
    if args.material not in MATERIAL_CLASSES:
        sys.exit(f"class must be one of: {', '.join(MATERIAL_CLASSES)}")
    if not any(r["n"] == args.n for r in rows):
        sys.exit(f"no corpus entry {args.n}")
    classes[str(args.n)] = args.material
    CLASSES.write_text(json.dumps(classes, indent=1, sort_keys=True))
    print(f"[{args.n}] class = {args.material}")


_REF_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pieria — e-ink calibration reference viewer</title>
<style>
  /* NEUTRAL SURROUND, DELIBERATELY.
     This page exists to be the ground truth in a colour judgement, so the page itself must not
     participate. A white background makes an image read darker and more saturated; a black one makes
     it read lighter and washed. Mid-grey is the standard evaluation surround (ISO 3664), and every
     piece of chrome here is strictly neutral — no accent colours anywhere near the artwork. */
  :root { --surround:#7a7a7a; --chrome:#4a4a4a; --ink:#f0f0f0; --dim:#c8c8c8; }
  * { box-sizing:border-box; margin:0; padding:0; }
  html,body { height:100%; }
  body {
    background:var(--surround); color:var(--ink);
    font:13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    display:flex; flex-direction:column; overflow:hidden;
  }
  header {
    background:var(--chrome); padding:8px 14px; display:flex; gap:16px; align-items:baseline;
    flex:0 0 auto; border-bottom:1px solid #333;
  }
  #n { font-size:20px; font-weight:700; min-width:4.5em; }
  #title { font-size:15px; }
  #coll, #panel { color:var(--dim); }
  /* The follow pill is the only non-neutral thing on the page and it is INTENTIONALLY outside the
     image area: if the page has silently stopped tracking the panel, that must be visible without
     looking away from the artwork for long. */
  #follow { margin-left:auto; padding:2px 8px; border-radius:3px; font-size:11px;
            background:#3a3a3a; color:var(--dim); border:1px solid #2a2a2a; }
  #follow.on { background:#d8d8d8; color:#222; }
  #hint { color:var(--dim); font-size:11px; text-align:right; }
  main { flex:1 1 auto; display:flex; align-items:center; justify-content:center; padding:14px; min-height:0; }
  img { max-width:100%; max-height:100%; object-fit:contain; display:block; }
  #feat {
    position:fixed; right:14px; bottom:14px; background:rgba(40,40,40,.94); padding:10px 12px;
    border-radius:4px; font-size:12px; display:none; white-space:pre; color:var(--dim);
  }
  #feat.on { display:block; }
  #jump {
    position:fixed; left:50%; top:50%; transform:translate(-50%,-50%);
    background:rgba(30,30,30,.95); padding:18px 28px; border-radius:6px;
    font-size:34px; letter-spacing:.1em; display:none;
  }
  #jump.on { display:block; }
</style>
</head>
<body>
<header>
  <span id="n">--/--</span>
  <span id="title">loading…</span>
  <span id="coll"></span>
  <span id="panel">__PANEL__</span>
  <span id="follow">follow: off</span>
  <span id="hint">← → step · number+Enter · <b>l</b> follow · <b>i</b> features · <b>f</b> fullscreen</span>
</header>
<main><img id="img" alt=""></main>
<div id="feat"></div>
<div id="jump"></div>

<script>
// Metadata is INLINED, not fetched: this page must still work when opened straight off the disk.
// FOLLOW mode needs http (fetch is blocked on file://), which is why `reference` prints a one-line
// http.server command — serve it FROM THE PI so the refs and the panel can never be different builds.
const EMBEDDED = __EMBEDDED__;

let META = [], idx = 0, buf = "", follow = false, lastN = null;
const $ = id => document.getElementById(id);

function render() {
  const m = META[idx];
  if (!m) return;
  $("img").src = m.file;
  $("img").alt = m.title;
  $("n").textContent = String(m.n).padStart(2, "0") + "/" + META.length;
  $("title").textContent = m.title;
  $("coll").textContent = m.collection;
  const f = m.features || {};
  $("feat").textContent =
    "wash    " + f.wash_pct +
    "\nlum     " + f.mean_lum +
    "\nchroma  " + f.mean_chroma +
    "\nedge    " + f.edge_pct +
    "\ncrop    " + (m.crop ? m.crop.map(v => v.toFixed(3)).join(", ") : "none") +
    (m.authored_box ? "  (AUTHORED)" : "");
  // Preload neighbours so stepping is instant — a 1600px JPEG decode is otherwise a visible stall,
  // and any pause invites the eye to re-adapt between the reference and the panel.
  [idx - 1, idx + 1].forEach(j => { if (META[j]) new Image().src = META[j].file; });
}
function go(i) { idx = Math.max(0, Math.min(META.length - 1, i)); render(); }
function goN(n) { const at = META.findIndex(m => m.n === n); if (at >= 0) go(at); }
function showJump() {
  const j = $("jump");
  if (buf) { j.textContent = buf; j.classList.add("on"); } else { j.classList.remove("on"); }
}
function setFollow(on, note) {
  follow = on;
  $("follow").textContent = "follow: " + (on ? "ON" : (note || "off"));
  $("follow").classList.toggle("on", on);
}
// Poll what the PANEL was last told to show. `full N` writes current.json, so the laptop tracks the
// panel instead of being stepped by hand — the two drifting apart mid-campaign is exactly the
// image-vs-judgement misalignment the frozen corpus exists to prevent, one level up.
async function poll() {
  if (!follow) return;
  try {
    const r = await fetch("current.json?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    const cur = await r.json();
    if (cur && cur.n && cur.n !== lastN) { lastN = cur.n; goN(cur.n); }
  } catch (e) {
    setFollow(false, "unavailable (open over http)");
  }
}
setInterval(poll, 1000);

addEventListener("keydown", e => {
  if (e.key === "ArrowRight" || e.key === " ") { go(idx + 1); e.preventDefault(); }
  else if (e.key === "ArrowLeft") { go(idx - 1); e.preventDefault(); }
  else if (e.key === "Home") go(0);
  else if (e.key === "End") go(META.length - 1);
  else if (e.key === "i") $("feat").classList.toggle("on");
  else if (e.key === "l") { setFollow(!follow); if (follow) { lastN = null; poll(); } }
  else if (e.key === "f") { document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen(); }
  else if (/^[0-9]$/.test(e.key)) { buf += e.key; showJump(); }
  else if (e.key === "Enter" && buf) {
    // Jump by SHEET NUMBER, not array position. They coincide today, but the corpus is appended to
    // (batch two was `extend`), so binding to n is what keeps "sheet 41" meaning sheet 41.
    goN(parseInt(buf, 10)); buf = ""; showJump();
  }
  else if (e.key === "Escape") { buf = ""; showJump(); }
});

META = EMBEDDED;
render();
setFollow(true);
</script>
</body>
</html>
"""


def cmd_target(args) -> None:
    """Render a self-calibrating measurement target and blit it.

    Every target carries a black registration frame (four inside corners -> homography) and a strip
    of PURE ink patches (-> per-photograph colour correction). That is what makes a handheld phone
    photo usable: the correction is solved from inside the same frame, so shots taken minutes apart
    under different light stay comparable. See tools/eink_target.py for why this is per-photo rather
    than one measured camera offset.
    """
    from tools import eink_target as et  # noqa: PLC0415
    w, h = args.width, args.height
    if args.kind == "art":
        if args.n is None:
            sys.exit("target art needs --n <corpus number>")
        rows = _load_corpus()
        row = next((r for r in rows if r["n"] == args.n), None)
        if row is None:
            sys.exit(f"no corpus entry {args.n}")
        img = Path(row["image"])
        cx0, cy0, cx1, cy1 = et.content_box(w, h)
        cw, ch = cx1 - cx0, cy1 - cy0
        crop, focal = _db_crop_and_focal(img.name, cw, ch)
        box = _authored_box(args.n)
        if box:
            crop = box
        # Fit to the CONTENT box, not the panel: the art must be measured at the size it is
        # photographed at, and rescaling a dithered frame afterwards would destroy the dither.
        fitted = ec.epaper._fit_rgb(img, cw, ch, args.fit, focal, crop)
        if args.white_point > 0:
            fitted = fitted.point([min(255, int(round(i * args.white_point))) for i in range(256)] * 3)
        if args.chroma_floor_max is not None:
            fitted = ec.epaper.apply_chroma_curve(fitted, args.chroma_gamma, args.chroma_floor_max,
                                                  args.chroma_hue_e0,
                                                  gap_normalised=args.chroma_gap_normalised,
                                                  floor_min=args.chroma_floor_min)
        elif abs(args.chroma_gamma - 1.0) > 1e-3:
            hue_c, sat_c, val_c = fitted.convert("HSV").split()
            lut = [min(255, int(round(255.0 * max((i / 255.0) ** args.chroma_gamma,
                                                  (i / 255.0) * args.chroma_floor))))
                   for i in range(256)]
            fitted = Image.merge("HSV", (hue_c, sat_c.point(lut), val_c)).convert("RGB")
        if abs(args.saturation - 1.0) > 1e-3:
            fitted = ImageEnhance.Color(fitted).enhance(args.saturation)
        if args.gamma > 0:
            fitted = ec.epaper._apply_gamma(fitted, args.gamma)
        content = et._quantize(fitted)
        # Save the UNQUANTISED fit as this render's reference. It has to be produced here, at this
        # exact framing: the art target fits into the content box, whose aspect differs from the
        # panel's, so a reference generated for the full panel is a different crop of the work and
        # cannot be compared against pixel for pixel.
        REF.mkdir(parents=True, exist_ok=True)
        ref_path = REF / f"artref_{args.n:02d}.jpg"
        fitted_ref = ec.epaper._fit_rgb(img, cw, ch, args.fit, focal, crop)
        fitted_ref.save(ref_path, "JPEG", quality=92)
        tag = f"art{args.n:02d}_g{args.gamma}_k{args.chroma_gamma}"
        if args.white_point > 0:
            tag += f"_wp{args.white_point}"
        if args.chroma_floor_max is not None:
            tag += (f"_hf{args.chroma_floor_max}gap" if args.chroma_gap_normalised
                    else f"_hf{args.chroma_floor_max}e{args.chroma_hue_e0}")
    else:
        # The render's pre-transform chain, in the same order cmd_full applies it. It must reach the
        # generator rather than the finished canvas: these levers act on CONTENT, and running them
        # over an already-dithered pattern would transform the dither instead. Undithered targets
        # (primaries, inkmix, uniformity, flat) ignore `pre` entirely — they are panel invariants
        # and no render setting applies to them, which is exactly why they are captured once.
        def _pre(im):
            # ⚠️ THE FULL CHAIN, IN cmd_full's ORDER: white-point -> chroma -> saturation -> gamma.
            # An earlier version applied only white-point and gamma while still ACCEPTING
            # --saturation and --chroma-gamma, so those two flags were silently inert. That is
            # exactly the EINK_SATURATION defect (ADR-089) — a knob that is offered, documented and
            # does nothing — and it would have turned a lever-interaction sweep into a set of
            # duplicate baselines that "proved" chroma has no effect.
            if args.white_point > 0:
                im = im.point([min(255, int(round(i * args.white_point))) for i in range(256)] * 3)
            if args.chroma_floor_max is not None:
                im = ec.epaper.apply_chroma_curve(im, args.chroma_gamma, args.chroma_floor_max,
                                                  args.chroma_hue_e0,
                                                  gap_normalised=args.chroma_gap_normalised,
                                                  floor_min=args.chroma_floor_min)
            elif abs(args.chroma_gamma - 1.0) > 1e-3:
                hue_c, sat_c, val_c = im.convert("HSV").split()
                lut = [min(255, int(round(255.0 * max((i / 255.0) ** args.chroma_gamma,
                                                      (i / 255.0) * args.chroma_floor))))
                       for i in range(256)]
                im = Image.merge("HSV", (hue_c, sat_c.point(lut), val_c)).convert("RGB")
            if abs(args.saturation - 1.0) > 1e-3:
                im = ImageEnhance.Color(im).enhance(args.saturation)
            if args.gamma > 0:
                im = ec.epaper._apply_gamma(im, args.gamma)
            return im

        lever_tag = ""
        if args.white_point > 0:
            lever_tag += f"_wp{args.white_point}"
        if args.gamma > 0:
            lever_tag += f"_g{args.gamma}"
        if abs(args.chroma_gamma - 1.0) > 1e-3:
            lever_tag += f"_k{args.chroma_gamma}"
        if args.chroma_floor_max is not None:
            lever_tag += f"_hf{args.chroma_floor_max}e{args.chroma_hue_e0}"
        if abs(args.saturation - 1.0) > 1e-3:
            lever_tag += f"_s{args.saturation}"

        if args.kind == "huevalue":
            content = et.target_huevalue(w, h, sat=args.sat, isolate=args.isolate, pre=_pre)
            tag = f"huevalue_s{args.sat}" + ("_iso" if args.isolate else "_joint") + lever_tag
        elif args.kind == "surround":
            content = et.target_surround(w, h, centre=args.centre, pre=_pre)
            tag = f"surround_c{args.centre}{lever_tag}"
        elif args.kind in ("primaries", "inkmix", "uniformity", "flat"):
            content = et.TARGETS[args.kind](w, h)
            tag = args.kind
        else:
            content = et.TARGETS[args.kind](w, h, pre=_pre)
            tag = f"{args.kind}{lever_tag}"

    canvas = et.compose(content, w, h, patches=(args.kind != "flat"))
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"target_{tag}_{w}x{h}.png"
    canvas.save(dest)
    print(f"target: {args.kind}  {w}x{h}")
    if args.kind == "art":
        print(f"  reference saved -> bench-eink/reference/artref_{args.n:02d}.jpg")
    print(f"  content box {et.content_box(w, h)}")
    print(f"  {dest}")
    if REF.exists():
        (REF / "current.json").write_text(json.dumps({"n": args.n or 0, "dest": dest.name}))
    if args.no_push:
        return
    from inky.auto import auto  # noqa: PLC0415
    panel = auto()
    pw, ph = panel.resolution
    shown = canvas if (canvas.width, canvas.height) == (pw, ph) else canvas.rotate(90, expand=True)
    panel.set_image(shown)
    panel.show()
    print("  pushed to panel")
    print("  PHOTO: straight on, diffuse light (the glass is specular), whole frame in shot "
          "including the black border and the patch strip.")


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
    fu.add_argument("--chroma-floor", type=float, default=0.0,
                    help="minimum multiplier applied to saturation (0 = no floor). Keeps faint colour "
                         "from being erased by an aggressive --chroma-gamma.")
    fu.add_argument("--chroma-gamma", type=float, default=1.0,
                    help="exponent on HSV saturation (s**k). >1 crushes low chroma while sparing "
                         "high chroma; 1.0 = off. Applied BEFORE --saturation.")
    fu.add_argument("--white-point", type=float, default=0.0,
                    help="scale input luminance before dithering so the brightest input lands on the "
                         "white INK rather than above it. 0.64 maps 255 -> 163 (the measured white "
                         "ink). 0 = off. Fixes highlight collapse, which gamma structurally cannot.")
    fu.add_argument("--chroma-floor-max", type=float, default=None,
                    help="HUE-CONDITIONED floor (ADR-088 correction). Replaces the scalar "
                         "--chroma-floor with floor(hue) = FLOOR_MAX * max(0, 1 - hue_err/E0), so "
                         "faint colour survives where an ink matches the hue and is crushed where "
                         "none does. Pass with --chroma-hue-e0.")
    fu.add_argument("--chroma-gap-normalised", action="store_true",
                    help="normalise hue error by the LOCAL ink gap instead of a fixed --chroma-hue-e0. "
                         "The inks are unevenly spaced, so a fixed cutoff annihilates 38%% of the hue "
                         "circle in the wide gaps while sparing the narrow warm arc entirely — "
                         "measured on the panel as a woodblock's pale blue vanishing (2026-08-28).")
    fu.add_argument("--chroma-floor-min", type=float, default=0.0,
                    help="floor never falls below this, so no hue is ever fully stripped of colour.")
    fu.add_argument("--chroma-hue-e0", type=float, default=20.0,
                    help="hue distance (PIL units, 256 = full circle) at which a hue counts as "
                         "unservable by any ink. Only used with --chroma-floor-max.")
    fu.add_argument("--width", type=int, default=1600)
    fu.add_argument("--height", type=int, default=1200)
    fu.add_argument("--fit", default="cover", choices=("cover", "contain"),
                    help="contain = letterbox the whole work onto white. On a panel whose substrate "
                         "IS paper-white that reads as a mount board, not as bars — the only way to "
                         "show portrait art on a landscape panel without cropping the composition.")
    fu.add_argument("--crop-key", default="auto",
                    choices=("auto", "none", "16:9", "9:16", "4:3", "3:4"),
                    help="auto = what production picks by nearest aspect; a key forces that box; "
                         "none = no authored crop at all (whole work).")
    fu.add_argument("--box", default="", help="explicit normalised crop x0,y0,x1,y1 (overrides --crop-key)")
    fu.add_argument("--save-box", action="store_true",
                    help="persist --box to boxes.json so `reference` frames the ground truth the "
                         "same way. Use it the moment a framing is accepted.")
    fu.add_argument("--no-push", action="store_true")

    rf = sub.add_parser("reference", help="regenerate the laptop reference set through the render's crop path")
    rf.add_argument("--width", type=int, default=1600)
    rf.add_argument("--height", type=int, default=1200)
    rf.add_argument("--fit", default="cover", choices=("cover", "contain"))
    rf.add_argument("--port", type=int, default=8090, help="port quoted in the printed serve command")

    fr = sub.add_parser("full-record", help="record a full-panel A/B verdict")
    fr.add_argument("n", type=int)
    fr.add_argument("verdict", choices=VERDICTS)
    fr.add_argument("--candidate", default="",
                    help="which recipe was the 'new' side, e.g. 'hue f0.70 e20' — a verdict with no "
                         "candidate cannot be replayed")
    fr.add_argument("--preference", default="", choices=("", "new", "incumbent", "same"),
                    help="which one the judge would rather HANG, when that differs from which is "
                         "closer to the reference. The verdict is always fidelity (the rig was "
                         "rebuilt around having a ground truth); this records the split so a "
                         "systematic divergence across the corpus becomes visible instead of being "
                         "argued from memory. Leave empty when fidelity and preference agree.")
    fr.add_argument("--note", default="", help="residual in the judge's own words; this is where the "
                                               "next mechanism has come from every time so far")
    fr.add_argument("--force", action="store_true", help="replace an existing record / skip the class gate")

    fs = sub.add_parser("full-status", help="campaign progress, by verdict and by material class")
    fs.add_argument("-v", "--verbose", action="store_true", help="list every work, not just the unjudged")

    tg = sub.add_parser("target", help="render a self-calibrating measurement target for photography")
    tg.add_argument("kind", choices=("primaries", "inkmix", "uniformity", "flat",
                                     "ramp", "tonefine", "huegrid", "huevalue", "surround",
                                     "edges", "linepairs", "resample", "art"))
    tg.add_argument("--isolate", action="store_true",
                    help="huevalue: dither each cell on its OWN and composite, instead of dithering "
                         "the whole grid in one pass. The DIFFERENCE between the two is the "
                         "measurement of how far Floyd-Steinberg error bleeds across cell "
                         "boundaries — i.e. how much any dithered grid target can be trusted.")
    tg.add_argument("--sat", type=float, default=0.55,
                    help="huevalue: saturation of the grid (0-1)")
    tg.add_argument("--centre", type=int, default=170,
                    help="surround: the input value repeated in every cell")
    tg.add_argument("--n", type=int, default=None, help="corpus number, for kind=art")
    tg.add_argument("--gamma", type=float, default=1.4)
    tg.add_argument("--saturation", type=float, default=1.0)
    tg.add_argument("--chroma-gamma", type=float, default=1.0)
    tg.add_argument("--white-point", type=float, default=0.0,
                    help="see `full --white-point` (ADR-090)")
    tg.add_argument("--chroma-floor", type=float, default=0.0)
    tg.add_argument("--chroma-floor-max", type=float, default=None)
    tg.add_argument("--chroma-hue-e0", type=float, default=20.0)
    tg.add_argument("--chroma-gap-normalised", action="store_true")
    tg.add_argument("--chroma-floor-min", type=float, default=0.0)
    tg.add_argument("--fit", default="cover", choices=("cover", "contain"))
    tg.add_argument("--width", type=int, default=1600)
    tg.add_argument("--height", type=int, default=1200)
    tg.add_argument("--no-push", action="store_true")

    cl = sub.add_parser("classify", help="pre-register a work's MATERIAL class (before judging it)")
    cl.add_argument("n", type=int, nargs="?", default=None)
    cl.add_argument("material", nargs="?", default=None)

    e = sub.add_parser("extend", help="append N more images, seeded with the existing corpus")
    e.add_argument("--n", type=int, default=30)
    e.add_argument("--scan-cap", type=int, default=12)

    sub.add_parser("status", help="what is judged and what is left")

    args = ap.parse_args()
    {"corpus": cmd_corpus, "show": cmd_show, "record": cmd_record,
     "status": cmd_status, "extend": cmd_extend, "full": cmd_full,
     "reference": cmd_reference, "full-record": cmd_full_record, "target": cmd_target,
     "full-status": cmd_full_status, "classify": cmd_classify}[args.cmd](args)


if __name__ == "__main__":
    main()
