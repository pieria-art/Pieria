"""
tools/derive_aspect_crops.py — derive per-aspect crop presets for catalog artworks
(maintainer tool — NOT part of the runtime image).

Each work can get up to four normalized boxes `[x0,y0,x1,y1]` (0..1), one per common screen shape,
keyed `"16:9" / "9:16" / "4:3" / "3:4"`, stored on the catalog item under `aspect_crops`. At render
time `epaper.pick_crop_for_aspect` picks the nearest preset for the target panel instead of computing
a focal-point cover crop — a focal point can only SLIDE a fixed-size window, it can't CHOOSE one, and
museum art clusters near-square so an uncomposed 9:16 crop of a square painting discards ~53% of it.

**Derivation (in-IDE, no model API call).** Exactly the `tools/fame_score.py --emit`/`--bake` pattern:
this tool does zero judgment itself. A Claude Code Sonnet fan-out LOOKS at a downscaled preview of each
work and picks the four boxes; this tool only does the file plumbing (emit a worklist + scratch
previews, then validate + bake the agent's results back into the catalog JSONs). There is no LLM API
call anywhere in this file.

**Derive against `_Library` masters, NOT `art-pack/_catalog_thumbs/` (verified, do not "optimize" this
back).** `_catalog_thumbs` is generated from the source image BEFORE `build_pack` applies Tier-1
`crop_box` (the photographed-frame trim), while `_Library` masters are the POST-`crop_box` bytes. For
the ~41 `needs_frame_crop` works those are two different coordinate spaces — a box an agent draws on
the thumb would land in the wrong place on the master. Masters are the ground truth the renderer
actually serves from, so that's what gets previewed and what the boxes are normalized against.

    python -m tools.derive_aspect_crops --emit worklist.json --limit 40 --collection american-art
    #   ...Sonnet looks at each worklist["file"] -> results.json:
    #   [{"source_url": "...", "aspect_crops": {"16:9": [x0,y0,x1,y1], "9:16": [...], ...}}]
    python -m tools.derive_aspect_crops --bake results.json              # dry-run: preview counts
    python -m tools.derive_aspect_crops --bake results.json --write      # apply to the catalog JSONs
    python -m tools.derive_aspect_crops --bake results.json --report qa.json --fail-under 95

Bake SNAPS every box to its exact target aspect first (preserving the agent's compositional intent —
see `snap()`), then QA-gates the corrected geometry (`aspect_inexact` / `not_maximal` / `focal_outside`,
plus `near_maximal` / `snap_moved_far` warnings) — see `_validate_item()`/`_gate_box()`. At the ~2857-work
scale nobody can eyeball every box, so `--report` dumps the full per-item verdict and `--fail-under`
lets an unattended run halt instead of baking a bad batch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image

# `epaper.py` lives at the repo root; tools/ scripts import it directly (house convention, see
# tools/eink_firstlight.py). The sys.path insert makes this robust even off `python -m` from root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from epaper import ASPECT_CROP_KEYS, normalize_crop_box  # noqa: E402
from tools.tag_resolution import index_masters  # noqa: E402

DEFAULT_DIRS = ["static/catalog", "art-pack/_catalog"]
DEFAULT_PACK = "art-pack"
DEFAULT_SCRATCH = Path(
    "/tmp/claude-1000/-home-josh-ai-workspace-Pieria/"
    "a5043e89-2e39-4669-9b58-98f2091e4b95/scratchpad/aspect_crops/imgs"
)
DOWNSCALE_EDGE = 600

# --- QA gate thresholds (post-snap) ---------------------------------------------------------------
ASPECT_INEXACT_TOL = 0.005   # post-snap real-aspect error tolerance; should never fire if snap is correct
NOT_MAXIMAL_REJECT = 0.90    # max(box w, box h) below this -> reject (throws away too much resolution)
NOT_MAXIMAL_WARN = 0.98      # below this (but >= reject floor) -> pass with a `near_maximal` warning
FOCAL_MARGIN = 0.05          # focal point within this fraction of a box edge -> too close, reject
SNAP_MOVED_FAR = 0.15        # max coordinate delta from snapping -> warn (not reject) that the agent
                              # may have misjudged the composition


def _hash8(source_url: str) -> str:
    return hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:8]


def _iter_catalog_items(dirs: list[str], collections: list[str] | None):
    """Yield (collection_id, item_dict) for every real collection file under `dirs`, in a stable
    (sorted-dir, sorted-file, in-file-order) order. Mirrors tag_resolution's skip rule."""
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.json")):
            if path.name == "index.json" or path.name.startswith("_"):
                continue
            doc = json.loads(path.read_text())
            items = doc if isinstance(doc, list) else doc.get("items", [])
            cid = doc.get("id", path.stem) if isinstance(doc, dict) else path.stem
            if collections and cid not in collections:
                continue
            for it in items:
                yield cid, it


def _has_all_aspect_crops(item: dict) -> bool:
    ac = item.get("aspect_crops")
    return isinstance(ac, dict) and all(k in ac for k in ASPECT_CROP_KEYS)


def _downscale(master_path: Path, out_path: Path, long_edge: int = DOWNSCALE_EDGE) -> None:
    with Image.open(master_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = long_edge / max(w, h)
        if scale < 1:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        im.save(out_path, "JPEG", quality=85)


# --------------------------------------------------------------------------- emit
def emit(out_path: Path, dirs: list[str], pack: Path, scratch: Path,
         limit: int | None, collections: list[str] | None) -> int:
    library = pack / "_Library"
    if not library.is_dir():
        print(f"FAIL: no masters at {library} — run tools.build_pack first.")
        return 1
    masters = index_masters(library)
    print(f"indexed {len(masters)} masters under {library}")
    scratch.mkdir(parents=True, exist_ok=True)

    worklist: list[dict] = []
    seen_urls: set[str] = set()
    already_done = no_master = 0
    for cid, it in _iter_catalog_items(dirs, collections):
        if limit and len(worklist) >= limit:
            break
        su = it.get("source_url")
        if not isinstance(su, str) or not su or su in seen_urls:
            continue
        if _has_all_aspect_crops(it):
            already_done += 1
            continue
        master = masters.get(_hash8(su))
        if master is None:
            no_master += 1
            continue
        seen_urls.add(su)
        with Image.open(master) as im:
            w0, h0 = im.size
        out_file = scratch / f"{_hash8(su)}.jpg"
        if not out_file.exists():
            _downscale(master, out_file)
        worklist.append({
            "source_url": su,
            "file": str(out_file.resolve()),
            "title": it.get("title", ""),
            "collection": cid,
            "master_size": [w0, h0],
        })

    out_path.write_text(json.dumps(worklist, indent=1, ensure_ascii=False))
    print(f"emitted {len(worklist)} works -> {out_path}  "
          f"(already had all 4: {already_done}, no locatable master: {no_master})")
    return 0


# --------------------------------------------------------------------------- bake
def _looks_full_frame(box) -> bool:
    """`normalize_crop_box` returns None for BOTH a malformed box and a legitimate near-full-frame box
    (its documented "already fills the frame" sentinel) — the two cases need different handling here
    (drop vs. store `[0,0,1,1]`), so this narrowly re-checks just the near-full-frame threshold that
    `normalize_crop_box` itself uses, WITHOUT reimplementing its shape/range/ordering validation (that
    stays solely `normalize_crop_box`'s job — this only runs after it has already returned None)."""
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return False
    return x0 <= 0.002 and y0 <= 0.002 and x1 >= 0.998 and y1 >= 0.998


def _target_ratio(key: str) -> float:
    kw, kh = key.split(":")
    return float(kw) / float(kh)


def _source_aspect(master_path: Path) -> float:
    with Image.open(master_path) as im:
        w, h = im.size
    return w / h


def snap(box, source_aspect: float, target: float) -> list[float]:
    """Return a box whose real aspect (`source_aspect * bw/bh`) == target, centred on `box`'s own
    centre, clamped to [0,1]. Ported from the throwaway `merge_snap.py` verified against degenerate
    input — this is the geometry-CORRECTION half of the bake gate: rather than trust (or reject) the
    agent's arithmetic, keep its compositional intent (the box centre) and solve the exact box.

    Steps: clamp+order the input box, take its centre, hold whichever relative dimension (w or h) is
    currently larger and solve the other so `source_aspect * bw/bh == target` exactly, scale both down
    if either would exceed 1.0, then re-centre and clamp into [0,1]."""
    x0, y0, x1, y1 = (float(v) for v in box)
    x0, x1 = sorted((max(0.0, x0), min(1.0, x1)))
    y0, y1 = sorted((max(0.0, y0), min(1.0, y1)))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    bw, bh = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)

    # want: source_aspect * bw/bh == target  ->  bw/bh == target/source_aspect
    r = target / source_aspect
    if bw >= bh * r:
        bh2 = min(1.0, bw / r)
        bw2 = bh2 * r
    else:
        bw2 = min(1.0, bh * r)
        bh2 = bw2 / r
    if bw2 > 1.0 or bh2 > 1.0:
        s = min(1.0 / bw2, 1.0 / bh2)
        bw2, bh2 = bw2 * s, bh2 * s

    nx0 = min(max(cx - bw2 / 2, 0.0), 1.0 - bw2)
    ny0 = min(max(cy - bh2 / 2, 0.0), 1.0 - bh2)
    return [round(nx0, 4), round(ny0, 4), round(nx0 + bw2, 4), round(ny0 + bh2, 4)]


def _parse_focal(fp) -> tuple[float, float] | None:
    """Parse a catalog item's flat 'focal_point': [x, y] into a clamped tuple, or None if absent/
    malformed (which the QA gate treats identically to "skip this check" — never a default centre)."""
    if isinstance(fp, (list, tuple)) and len(fp) == 2:
        try:
            x, y = float(fp[0]), float(fp[1])
        except (TypeError, ValueError):
            return None
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            return x, y
    return None


def _gate_box(snapped: list[float], source_aspect: float, target: float,
              focal_point: tuple[float, float] | None) -> tuple[str | None, list[str]]:
    """QA-gate one already-snapped box. Returns (reject_reason | None, warnings)."""
    x0, y0, x1, y1 = snapped
    bw, bh = x1 - x0, y1 - y0
    warnings: list[str] = []

    real_aspect = source_aspect * bw / max(bh, 1e-9)
    if abs(real_aspect - target) / target > ASPECT_INEXACT_TOL:
        # Should never fire post-snap — if it does, the snap arithmetic itself is broken.
        return "aspect_inexact", warnings

    m = max(bw, bh)
    if m < NOT_MAXIMAL_REJECT:
        return "not_maximal", warnings
    if m < NOT_MAXIMAL_WARN:
        warnings.append("near_maximal")

    if focal_point is not None and bw > 0 and bh > 0:
        fx, fy = focal_point
        # focal_x/y == 0.5 is a documented SENTINEL ("no single clear subject" — agents.py's
        # FOCAL_POINT_INSTRUCTION), not a measurement, so an axis pinned at exactly 0.5 carries no
        # compositional signal and must be skipped — checking it would reject correctly-composed
        # landscape crops that legitimately slide off a "no opinion" axis. A near-0.5 value like 0.48
        # IS a real measurement and stays checked (exact-equality only, no tolerance).
        bad_x = fx != 0.5 and not (FOCAL_MARGIN <= (fx - x0) / bw <= 1 - FOCAL_MARGIN)
        bad_y = fy != 0.5 and not (FOCAL_MARGIN <= (fy - y0) / bh <= 1 - FOCAL_MARGIN)
        if bad_x or bad_y:
            return "focal_outside", warnings

    return None, warnings


def _validate_item(aspect_crops: dict, source_aspect: float,
                    focal_point: tuple[float, float] | None = None,
                    diag: dict | None = None) -> tuple[dict | None, str | None]:
    """Validate one item's aspect_crops dict: SNAP each box to its exact target aspect (preserving
    compositional intent — see `snap()`), then QA-gate the corrected geometry (see `_gate_box()`).

    TWO different atomicity rules, deliberately not the same:

    - MALFORMED input (wrong arity, non-numeric, out of [0,1] range/order) is untrustworthy — we can't
      tell what the agent MEANT, so it voids the WHOLE item (mirrors
      tests/test_focal.py::test_malformed_focal_point_is_atomic_and_ignored): returns (None,
      "malformed") regardless of how good the other three boxes are. Checked across ALL keys before
      returning, so a malformed key doesn't hide behind an earlier good one.
    - A QUALITY gate rejection (aspect_inexact / not_maximal / focal_outside) drops only the
      OFFENDING key — a dropped key just has no preset and the renderer falls back to its existing
      focal-cover crop for that one shape, which is a strict improvement over discarding three
      perfectly good sibling boxes over one borderline one. Per-key drop reasons land in `diag`
      (`diag[key]["reject"]`), not in the return value. If every key ends up dropped this way, the
      item as a whole produced nothing usable: returns (None, "empty").

    On success (>=1 surviving key) returns (dict_of_surviving_keys, None) — note this can be a PARTIAL
    dict of 1-4 keys, not necessarily all four the agent supplied.

    `source_aspect` (master width/height) is required because normalized [0,1] box coordinates are
    ANISOTROPIC: a box's raw (x1-x0)/(y1-y0) is only the box's real on-screen aspect once rescaled by
    the source image's own aspect (real_aspect = source_aspect * raw_ratio). `snap()` folds this into
    its target-ratio solve; `_gate_box()`'s aspect_inexact check uses the same formula, so if snap ever
    solved the wrong (raw, un-rescaled) ratio the gate would catch it.

    `focal_point`, if given, feeds the `focal_outside` gate (skipped when None). `diag`, if given, is
    populated per-key with `{"box", "snap_delta", "warnings", "reject"}` for reporting."""
    out: dict[str, list[float]] = {}
    to_gate: dict[str, tuple] = {}
    malformed = False

    # Pass 1: shape/range validation for every key, deciding atomicity. Full-frame keys are accepted
    # outright (no gate — "already fills the frame" is definitionally maximal and needs no snapping);
    # everything else either queues for the quality gate below or marks the whole item untrustworthy.
    for key in ASPECT_CROP_KEYS:
        if key not in aspect_crops:
            continue
        box = aspect_crops[key]
        normalized = normalize_crop_box(box)
        if normalized is None:
            if _looks_full_frame(box):
                out[key] = [0.0, 0.0, 1.0, 1.0]
                if diag is not None:
                    diag[key] = {"box": out[key], "snap_delta": 0.0, "warnings": [], "reject": None,
                                 "full_frame": True}
                continue
            malformed = True
            if diag is not None:
                diag[key] = {"box": None, "snap_delta": None, "warnings": [], "reject": "malformed"}
            continue
        to_gate[key] = normalized

    if malformed:
        return None, "malformed"

    # Pass 2: snap + quality-gate each remaining key independently — a rejection here drops only that
    # key, never the whole item.
    for key, normalized in to_gate.items():
        target = _target_ratio(key)
        snapped = snap(normalized, source_aspect, target)
        delta = max(abs(a - b) for a, b in zip(normalized, snapped))
        reason, warnings = _gate_box(snapped, source_aspect, target, focal_point)
        if delta > SNAP_MOVED_FAR:
            warnings.append("snap_moved_far")
        if diag is not None:
            diag[key] = {"box": snapped, "snap_delta": round(delta, 4), "warnings": warnings,
                         "reject": reason}
        if reason is not None:
            continue  # drop only this key
        out[key] = snapped

    if not out:
        return None, "empty"
    return out, None


def _index_focal_points(dirs: list[str]) -> dict[str, dict]:
    """source_url -> catalog item, first match wins. Used to look up each work's `focal_point` for the
    `focal_outside` gate — deliberately the catalog item, not anything the results file itself claims,
    same non-trust posture as `_source_aspect` reading the on-disk master."""
    by_url: dict[str, dict] = {}
    for _cid, it in _iter_catalog_items(dirs, None):
        su = it.get("source_url")
        if isinstance(su, str) and su and su not in by_url:
            by_url[su] = it
    return by_url


def _load_validated(
    results_path: Path, masters: dict[str, Path], su_index: dict[str, dict],
) -> tuple[dict[str, dict], dict[str, int], dict[str, int], dict[str, int], list[dict]]:
    """Validate every result. Source aspect is ALWAYS derived from the actual on-disk master (matched
    via the same `index_masters` join `--emit` used) — never trusted from the results file — so a
    result can't misreport its own source dimensions to sneak past the aspect check.

    Returns (validated, rejects, drop_counts, warn_counts, report_rows):
    - `validated`: source_url -> the surviving aspect_crops dict (1-4 keys — may be PARTIAL; see
      `_validate_item`'s two-tier atomicity).
    - `rejects`: ITEM-level counts (`bad_entry` / `no_master` / `malformed` / `empty`) — items that
      produced zero usable boxes, for whatever reason.
    - `drop_counts`: individual BOX-level counts (`not_maximal` / `focal_outside` / `aspect_inexact`)
      dropped by the quality gate, tallied across every item regardless of whether the item as a whole
      still ended up usable — this is deliberately separate from `rejects` because a gate drop does
      NOT void its item.
    - `report_rows`: the full per-item QA detail (see `--report`), independent of pass/fail, so a
      large run's quality can be read back without eyeballing images."""
    raw = json.loads(results_path.read_text())
    validated: dict[str, dict] = {}
    rejects: dict[str, int] = {}
    drop_counts: dict[str, int] = {}
    warn_counts: dict[str, int] = {}
    report_rows: list[dict] = []
    aspect_cache: dict[str, float] = {}
    for r in raw:
        su = r.get("source_url")
        ac = r.get("aspect_crops")
        title = r.get("title") or (su_index.get(su, {}).get("title") if isinstance(su, str) else None) or ""
        if not isinstance(su, str) or not su or not isinstance(ac, dict):
            rejects["bad_entry"] = rejects.get("bad_entry", 0) + 1
            report_rows.append({"source_url": su, "title": title, "valid": False,
                                 "reject_reason": "bad_entry", "keys": {}})
            continue
        h = _hash8(su)
        if h not in aspect_cache:
            master = masters.get(h)
            if master is None:
                rejects["no_master"] = rejects.get("no_master", 0) + 1
                report_rows.append({"source_url": su, "title": title, "valid": False,
                                     "reject_reason": "no_master", "keys": {}})
                continue
            aspect_cache[h] = _source_aspect(master)
        focal_point = _parse_focal(su_index.get(su, {}).get("focal_point"))
        diag: dict = {}
        out, reason = _validate_item(ac, aspect_cache[h], focal_point, diag)
        for key_diag in diag.values():
            for w in key_diag.get("warnings", []):
                warn_counts[w] = warn_counts.get(w, 0) + 1
            drop = key_diag.get("reject")
            if drop and drop != "malformed":
                drop_counts[drop] = drop_counts.get(drop, 0) + 1
        report_rows.append({"source_url": su, "title": title, "valid": reason is None,
                             "reject_reason": reason, "keys": diag})
        if reason is not None:
            rejects[reason] = rejects.get(reason, 0) + 1
            continue
        validated[su] = out
    return validated, rejects, drop_counts, warn_counts, report_rows


def bake(results_path: Path, dirs: list[str], pack: Path, write: bool,
         report_path: Path | None = None, fail_under: float | None = None) -> int:
    library = pack / "_Library"
    if not library.is_dir():
        print(f"FAIL: no masters at {library} — run tools.build_pack first.")
        return 1
    masters = index_masters(library)
    su_index = _index_focal_points(dirs)
    validated, rejects, drop_counts, warn_counts, report_rows = _load_validated(
        results_path, masters, su_index)
    # `rejects` = items that produced ZERO usable boxes (bad_entry/no_master fail before we even try;
    # `malformed` voids atomically; `empty` means every key was gate-dropped). `drop_counts` = individual
    # boxes the quality gate dropped from an otherwise-still-usable item — NOT items, never atomic.
    total_rejected = sum(rejects.values())
    total = len(validated) + total_rejected
    pct = 100.0 * len(validated) / total if total else 100.0
    total_dropped_boxes = sum(drop_counts.values())
    print(f"loaded {results_path}: {len(validated)} item(s) usable, {total_rejected} fully voided"
          + (f"  ({pct:.1f}% usable)" if total else ""))
    for reason in sorted(rejects):
        print(f"  reject[{reason}]: {rejects[reason]}")
    print(f"boxes dropped by quality gate (siblings kept): {total_dropped_boxes}")
    for reason in sorted(drop_counts):
        print(f"  drop[{reason}]: {drop_counts[reason]}")
    if warn_counts:
        print("warnings:")
        for kind in sorted(warn_counts):
            print(f"  warn[{kind}]: {warn_counts[kind]}")

    if report_path is not None:
        report_path.write_text(json.dumps(report_rows, indent=1, ensure_ascii=False))
        print(f"wrote QA report ({len(report_rows)} item(s)) -> {report_path}")

    if fail_under is not None and pct < fail_under:
        print(f"\nFAIL: valid rate {pct:.1f}% is below --fail-under {fail_under:.1f}% — refusing to bake.")
        return 1

    items_changed = boxes_written = 0
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.json")):
            if path.name == "index.json" or path.name.startswith("_"):
                continue
            doc = json.loads(path.read_text())
            items = doc if isinstance(doc, list) else doc.get("items", [])
            file_changed = False
            for it in items:
                su = it.get("source_url")
                if su not in validated:
                    continue
                new_ac = validated[su]
                cur_ac = it.get("aspect_crops") if isinstance(it.get("aspect_crops"), dict) else {}
                diff_keys = [k for k in new_ac if cur_ac.get(k) != new_ac[k]]
                if not diff_keys:
                    continue  # already applied — idempotent no-op
                items_changed += 1
                boxes_written += len(diff_keys)
                file_changed = True
                if write:
                    merged = dict(cur_ac)
                    merged.update(new_ac)
                    it["aspect_crops"] = merged
            if file_changed and write:
                path.write_text(json.dumps(doc, indent=1, ensure_ascii=False))

    print(f"\n{'wrote' if write else 'would write'}: {items_changed} item(s), {boxes_written} box(es)"
          + ("" if write else "  (dry-run — re-run with --write to apply)"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit", type=Path, metavar="OUT", help="dump the worklist + previews for Sonnet to look at")
    g.add_argument("--bake", type=Path, metavar="RESULTS", help="write aspect_crops from Sonnet's results")
    ap.add_argument("--dir", action="append", dest="dirs", help=f"catalog dir(s). Default: {DEFAULT_DIRS}")
    ap.add_argument("--pack", type=Path, default=Path(DEFAULT_PACK), help="built pack dir (has _Library/)")
    ap.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH, help="--emit: downscaled preview dir")
    ap.add_argument("--limit", type=int, default=None, help="--emit: cap the worklist size")
    ap.add_argument("--collection", action="append", dest="collections",
                     help="--emit: restrict to this collection id (repeatable)")
    ap.add_argument("--write", action="store_true", help="--bake: apply (default: dry-run)")
    ap.add_argument("--report", type=Path, metavar="PATH",
                     help="--bake: write a JSON array of every per-item QA outcome")
    ap.add_argument("--fail-under", type=float, metavar="PCT", default=None,
                     help="--bake: exit non-zero (and skip --write) if the valid-item rate is below PCT")
    args = ap.parse_args()

    dirs = args.dirs or DEFAULT_DIRS
    if args.emit:
        return emit(args.emit, dirs, args.pack, args.scratch, args.limit, args.collections)
    return bake(args.bake, dirs, args.pack, args.write, args.report, args.fail_under)


if __name__ == "__main__":
    raise SystemExit(main())
