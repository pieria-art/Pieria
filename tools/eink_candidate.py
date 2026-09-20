"""
tools/eink_candidate.py — ADR-084 full-panel judgement: shipping vs measured-palette barycentric.
(maintainer tool — NOT part of the runtime image)

WHY THIS EXISTS. ADR-117 wired the measured inks (ADR-116) into the analysis plane, but the shipping
quantiser still measures distance against the vendor swatch (`SPECTRA6_DITHER_PALETTE`) — a different,
un-measured panel. Whether to replace it is ADR-084's call, made ON THE PANEL, on real works, not from a
metric. This tool renders both candidates for a small session of works, side by side, framed EXACTLY as
production, and blinds the pairing so the judgement isn't contaminated by knowing which is which.

TWO CANDIDATES, per work:
    A — shipping: `epaper.render_for_epaper(...)`, decoded back to an ink-index map. Byte-identical to
        what `/display/{id}/current.png` would return today — no lever chain is re-implemented here.
    B — measured-palette barycentric: the same framed pixels, gamut-mapped onto the panel's ACHIEVABLE
        gamut in Lab (`eink_gamut.map_srgb8`, derived neutral floor + black-point toe by default), then
        decomposed into ink weights and realised one ink per pixel by noise-threshold sampling
        (`eink_barycentric.dither`) — NOT Floyd-Steinberg, and NOT a second remap: `dither` already
        decomposes against `eink_panel_model.ink_xyz()` (the measured inks) in linear light, which is
        the whole point of doing gamut mapping and dithering against the SAME palette.

FRAMING is whatever production would pick: `eink_bench._db_crop_and_focal` (the DB's `aspect_crops` +
focal point, ADR-055), then an `_authored_box` override when the work carries one — and only when it
was given as a corpus number, since an authored box is keyed to a corpus `n`, not a bare library path.

BLINDING. `--out/blinding.json` is the only place the A/B identity is recorded; everywhere else (the
viewer, the push list) a work's two renders carry only a session-local "L"/"R" label, assigned by a
deterministic draw from `--seed` — so the same seed over the same work list reproduces the same
assignment, but a human never sees "A" or "B" while judging.

    python -m tools.eink_candidate --work 9 --work 52 --work 33 --work 15 --work 1 \
        --work Artwork/_Library/dutch-golden-age__the-night-watch__ff740524.jpg \
        --out bench-eink/analysis/session_2026-09-20 --seed 20260920
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zlib
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import epaper as ep  # noqa: E402
from tools import eink_barycentric as eba  # noqa: E402
from tools import eink_bench as eb  # noqa: E402
from tools import eink_gamut as eg  # noqa: E402
from tools import eink_panel_model as pm  # noqa: E402

CORPUS = Path("bench-eink/corpus.json")

#: Deliberately mirrors `eink_bench` defaults (1600x1200) so a candidate session matches production
#: judgement conditions unless overridden.
DEFAULT_W = 1600
DEFAULT_H = 1200


def _load_corpus() -> list[dict] | None:
    if not CORPUS.exists():
        return None
    return json.loads(CORPUS.read_text())


def resolve_work(spec: str, rows: list[dict] | None) -> tuple[int | None, Path]:
    """A corpus number or a library path -> (n or None, the image path).

    A bare integer is always read as a corpus `n` (never a filename that happens to be all digits —
    the corpus is the authoritative session list and library filenames carry a content hash, so no
    real filename collides with this).
    """
    try:
        n = int(spec)
    except ValueError:
        n = None
    if n is not None:
        if not rows:
            sys.exit(f"--work {spec} looks like a corpus number but {CORPUS} was not found")
        row = next((r for r in rows if r["n"] == n), None)
        if row is None:
            sys.exit(f"no corpus entry {n} (have 1..{max(r['n'] for r in rows)})")
        return n, Path(row["image"])
    path = Path(spec)
    if not path.exists():
        sys.exit(f"no such image: {path}")
    return None, path


def slug_for(path: Path) -> str:
    """`collection__title` from a `_Library` filename `collection__title__hash.jpg`, hash dropped.

    Filesystem-safe by construction (the library naming convention already is); falls back to the bare
    stem for a path that doesn't follow it.
    """
    parts = Path(path).stem.split("__")
    return "__".join(parts[:2]) if len(parts) >= 2 else Path(path).stem


def frame_work(path: Path, n: int | None, w: int, h: int) -> tuple[tuple | None, tuple, bool]:
    """(crop, focal, authored) exactly as production would pick, for THIS work at w x h.

    `_authored_box` only applies when the work was named by corpus number — an authored box is keyed
    to a corpus `n` (`bench-eink/boxes.json`), so a bare library path never has one to look up.
    """
    crop, focal = eb._db_crop_and_focal(path.name, w, h)
    authored = False
    if n is not None:
        box = eb._authored_box(n)
        if box is not None:
            crop, authored = box, True
    return crop, focal, authored


# --- Variant A: the shipping renderer, decoded back to an ink-index map ----------------------------

def variant_a_index(path: Path, w: int, h: int, focal: tuple, crop: tuple | None) -> tuple[np.ndarray, np.ndarray]:
    """(ink index map, decoded RGB array) from `epaper.render_for_epaper` — the shipping bytes,
    byte-identical to what `/display/{id}/current.png` returns today. Nothing here re-implements the
    lever chain; it only decodes what that function already produced."""
    png = ep.render_for_epaper(
        path, w, h, palette="spectra6", fit="cover", focal=focal, fmt="png",
        enhance=True, crop_box=tuple(crop) if crop else None,
    )
    arr = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.uint8)
    idx = _index_from_output_rgb(arr)
    return idx, arr


def _index_from_output_rgb(arr: np.ndarray) -> np.ndarray:
    """Map each pixel's RGB back to its `SPECTRA6_OUTPUT_PALETTE` index. Asserts every pixel maps —
    the shipping renderer re-encodes to these six PURE primaries, so anything else means the decode
    (or the renderer) produced a colour this palette doesn't contain."""
    idx = np.full(arr.shape[:2], -1, dtype=np.int16)
    for i, rgb in enumerate(ep.SPECTRA6_OUTPUT_PALETTE):
        idx[np.all(arr == np.array(rgb, dtype=np.uint8), axis=-1)] = i
    unmapped = int((idx < 0).sum())
    if unmapped:
        raise AssertionError(
            f"variant A: {unmapped} pixel(s) did not map to any SPECTRA6_OUTPUT_PALETTE ink")
    return idx.astype(np.uint8)


# --- Variant B: measured-palette barycentric ---------------------------------------------------------

def blue_noise(h: int, w: int, seed: int, salt: str) -> np.ndarray:
    """Deterministic interleaved-gradient-noise field (Jimenez — the same construction
    `eink_barycentric._ign` uses by default), phase-shifted by `--seed` and the work's slug.

    `eink_barycentric.dither`'s default noise (`_ign` with no offset) is already deterministic, but it
    is the SAME field for every call — every work would threshold-sample against identical noise,
    which is an unnecessary correlation across a blind session. Shifting the IGN lattice by a
    seed+salt-derived phase keeps each work decorrelated from the others while making the whole session
    reproducible from one `--seed` (same seed + same work list -> identical files).
    """
    rng = np.random.default_rng([int(seed) & 0xFFFFFFFF, zlib.crc32(salt.encode())])
    ox, oy = rng.random(2) * 4096.0
    y, x = np.mgrid[0:h, 0:w].astype(np.float64)
    return np.modf(52.9829189 * np.modf(0.06711056 * (x + ox) + 0.00583715 * (y + oy))[0])[0]


def variant_b_index(fitted_rgb: Image.Image, seed: int, salt: str) -> np.ndarray:
    """The measured-palette barycentric ink-index map for an already-FRAMED (not yet enhanced/
    quantised) RGB image: gamut-map onto the panel's achievable gamut in Lab (derived floor + toe,
    ADR-117 defaults), then decompose into ink weights and realise one ink per pixel — no second
    mapping, no Floyd-Steinberg: `eink_barycentric.dither` already decomposes against
    `eink_panel_model.ink_xyz()` (the measured inks) in linear light."""
    rgb8 = np.asarray(fitted_rgb.convert("RGB"), dtype=np.uint8)
    lab = eg.map_srgb8(rgb8)
    q = eg.to_quantiser_srgb8(lab)
    noise = blue_noise(rgb8.shape[0], rgb8.shape[1], seed, salt)
    return eba.dither(q, noise=noise)


# --- Shared: ink fractions, panel-ready encode, laptop preview --------------------------------------

def ink_fractions(idx: np.ndarray) -> dict:
    """% of pixels per ink, 1 decimal, in `eink_panel_model.INK_NAMES` order."""
    total = idx.size
    return {name: round(100.0 * float(np.count_nonzero(idx == i)) / total, 1)
            for i, name in enumerate(pm.INK_NAMES)}


def panel_rgb(idx: np.ndarray) -> Image.Image:
    """Ink-index map -> a panel-ready RGB image, encoded exactly the way `eink_bench.cmd_full` encodes
    what it pushes: quantise-to-`SPECTRA6_OUTPUT_PALETTE`'s pure primaries, then `.convert("RGB")` —
    the same `out` it hands to `panel.set_image()`. Built directly from the index map (no re-dither):
    a `P`-mode canvas carrying the output palette, converted to RGB."""
    im = Image.new("P", (idx.shape[1], idx.shape[0]))
    im.putpalette(ep._flat_palette(ep.SPECTRA6_OUTPUT_PALETTE))
    im.putdata(idx.astype(np.uint8).reshape(-1).tolist())
    return im.convert("RGB")


def preview_rgb(idx: np.ndarray) -> Image.Image:
    """Ink-index map -> an RGB image in the MEASURED ink colours (`eink_panel_model.ink_srgb8()`), for
    laptop viewing — what the shipping/barycentric palettes disagree about (ADR-117) is precisely
    hidden if previewed in the swatch's own pure primaries instead."""
    inks = pm.ink_srgb8()
    return Image.fromarray(inks[idx.astype(np.int64)], mode="RGB")


# --- Session driver -----------------------------------------------------------------------------------

def _title_of(image_path) -> tuple[str, str]:
    return eb._title_of(str(image_path))


def run_session(work_specs: list[str], out: Path, w: int, h: int, seed: int) -> list[dict]:
    rows = _load_corpus()
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    results = []
    for spec in work_specs:
        n, path = resolve_work(spec, rows)
        slug = slug_for(path)
        crop, focal, authored = frame_work(path, n, w, h)
        wdir = out / slug
        wdir.mkdir(parents=True, exist_ok=True)

        idx_a, arr_a = variant_a_index(path, w, h, focal, crop)
        fitted_b = ep._fit_rgb(path, w, h, "cover", focal, crop)
        idx_b = variant_b_index(fitted_b, seed, slug)

        panel_rgb(idx_a).save(wdir / "A_shipping.png")
        panel_rgb(idx_b).save(wdir / "B_barycentric.png")
        preview_rgb(idx_a).save(wdir / "A_preview.png")
        preview_rgb(idx_b).save(wdir / "B_preview.png")
        # Same framed pixels variant B ran on, unenhanced/unquantised — the ground truth for both,
        # produced through the render's own crop path (like `eink_bench.cmd_reference`).
        fitted_b.save(wdir / "reference.jpg", "JPEG", quality=92)

        fracs_a, fracs_b = ink_fractions(idx_a), ink_fractions(idx_b)
        (wdir / "inks.json").write_text(json.dumps({"A": fracs_a, "B": fracs_b}, indent=1))

        # Blind assignment: one draw per work, from the session RNG, in the given work order — the
        # entire reason `--seed` + the same work list reproduces the same files.
        a_is_left = bool(rng.integers(0, 2))
        labels = {"L": "A" if a_is_left else "B", "R": "B" if a_is_left else "A"}
        # Which line comes first in pushlist.txt is a SEPARATE draw — "the two labels per work in
        # randomised order" is about push order, not about which side of the viewer is A or B.
        l_first = bool(rng.integers(0, 2))

        coll, title = _title_of(path)
        results.append({
            "slug": slug, "n": n, "image": str(path), "collection": coll, "title": title,
            "crop": list(crop) if crop else None, "authored_box": authored,
            "labels": labels, "l_first": l_first,
            "fracs": {"A": fracs_a, "B": fracs_b},
        })
        print(f"[{slug}] n={n}  crop={crop if crop else 'none (focal cover)'}")
        print(f"  A (shipping):     {fracs_a}")
        print(f"  B (barycentric):  {fracs_b}")

    (out / "blinding.json").write_text(json.dumps({
        "seed": seed,
        "works": [{"slug": r["slug"], "n": r["n"], "image": r["image"], "crop": r["crop"],
                   "authored_box": r["authored_box"], "labels": r["labels"]} for r in results],
    }, indent=1))

    lines = []
    for r in results:
        by_label = {v: k for k, v in r["labels"].items()}   # "A"/"B" -> "L"/"R"
        variant_file = {"A": "A_shipping.png", "B": "B_barycentric.png"}
        order = (r["labels"]["L"], r["labels"]["R"]) if r["l_first"] else (r["labels"]["R"], r["labels"]["L"])
        for variant in order:
            label = by_label[variant]
            lines.append(f"{r['slug']} {label} {r['slug']}/{variant_file[variant]}")
    (out / "pushlist.txt").write_text("\n".join(lines) + "\n")

    (out / "index.html").write_text(_index_html(results))
    return results


def _index_html(results: list[dict]) -> str:
    blocks = []
    for r in results:
        left_variant, right_variant = r["labels"]["L"], r["labels"]["R"]
        blocks.append(f"""
  <section class="work">
    <h2>{r['title']} <span class="coll">{r['collection']}</span></h2>
    <div class="row">
      <figure><img src="{r['slug']}/reference.jpg" alt="reference"><figcaption>Reference</figcaption></figure>
      <figure><img src="{r['slug']}/{'A' if left_variant == 'A' else 'B'}_preview.png" alt="left">
        <figcaption>Left</figcaption></figure>
      <figure><img src="{r['slug']}/{'A' if right_variant == 'A' else 'B'}_preview.png" alt="right">
        <figcaption>Right</figcaption></figure>
    </div>
  </section>""")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pieria — ADR-084 candidate viewer</title>
<style>
  body {{ background:#7a7a7a; color:#f0f0f0; font:14px/1.4 system-ui, sans-serif; margin:0; padding:20px; }}
  h2 {{ font-size:16px; margin:24px 0 8px; }}
  .coll {{ color:#c8c8c8; font-size:12px; font-weight:normal; margin-left:8px; }}
  .row {{ display:flex; gap:14px; flex-wrap:wrap; }}
  figure {{ margin:0; background:#4a4a4a; padding:8px; border-radius:4px; }}
  figure img {{ display:block; max-width:360px; max-height:270px; }}
  figcaption {{ text-align:center; font-size:12px; color:#c8c8c8; margin-top:4px; }}
</style>
</head>
<body>
  <p>Blind viewer — the truth (which side is A/B) lives only in <code>blinding.json</code>, never here.</p>
  {''.join(blocks)}
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", action="append", required=True, dest="works",
                    help="a corpus number (bench-eink/corpus.json) or a library image path; repeatable")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--width", type=int, default=DEFAULT_W)
    ap.add_argument("--height", type=int, default=DEFAULT_H)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    run_session(args.works, args.out, args.width, args.height, args.seed)
    print(f"\n{len(args.works)} works -> {args.out}/")
    print(f"  {args.out}/blinding.json  {args.out}/pushlist.txt  {args.out}/index.html")


if __name__ == "__main__":
    main()
