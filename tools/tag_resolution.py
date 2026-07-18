"""
tools/tag_resolution.py — stamp each catalog work with an honest resolution tier (HD / 4K / 8K)
(maintainer tool — NOT part of the runtime image). Implements `.ai/spec_resolution_tags.md`.

The browse grid looks identical whether a work is a crisp 8K master or a grandfathered ~2400px scan.
This pass measures the resolution the user actually gets and writes a small tag so the UI is honest.

**Measurement (offline, 100% coverage).** The pack caps every master at `DISPLAY_MAX_EDGE = 7680` and
never upscales, so a built master's long edge **is** the delivered resolution — its bucket is the tier.
We read the already-built masters under `art-pack/_Library/` (needs a prior `tools.build_pack` run), so
no network probe is required. Masters are matched by the stable `sha1(source_url)[:8]` suffix in their
filename — robust to the title-slug portion changing.

**Tiers** (native long edge, aligned 1:1 to the existing floors — no new magic numbers):
    HD  <3840  · 4K  3840–7679  · 8K  ≥7680

Writes two additive fields per item — `resolution_tier` ("HD"|"4K"|"8K") and `delivered_edge` (int px) —
and a per-collection `min_tier` into `static/catalog/index.json` (the "4K+" pack-card roll-up: a
collection promises its weakest member's tier). Idempotent; additive only.

    python -m tools.tag_resolution                 # dry-run: tier histogram, write nothing
    python -m tools.tag_resolution --write         # apply to static/catalog/ + art-pack/_catalog/
    python -m tools.tag_resolution --pack ./art-pack --dir static/catalog   # non-default paths
"""
import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image

DEFAULT_DIRS = ["static/catalog", "art-pack/_catalog"]
DEFAULT_PACK = "art-pack"

# Tier thresholds (long edge, px). 3840 = the true-4K floor; 7680 = the 8K display cap (ADR-030).
TIER_4K = 3840
TIER_8K = 7680
_TIER_ORDER = {"HD": 0, "4K": 1, "8K": 2}


def tier_for(edge: int) -> str:
    if edge >= TIER_8K:
        return "8K"
    if edge >= TIER_4K:
        return "4K"
    return "HD"


def _hash8(source_url: str) -> str:
    return hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:8]


def index_masters(library: Path) -> dict[str, Path]:
    """Map `sha1(source_url)[:8]` -> master path by parsing the `…__<hash8>.jpg` filename suffix
    (build_pack's master_filename). The hash is title-independent, so this survives a title re-slug."""
    out: dict[str, Path] = {}
    for f in library.glob("*.jpg"):
        stem = f.stem
        if "__" in stem:
            out.setdefault(stem.rsplit("__", 1)[-1], f)
    return out


def _edge(path: Path) -> int:
    with Image.open(path) as im:
        return max(im.size)


def tag_items(items: list[dict], masters: dict[str, Path], cache: dict[str, int]) -> tuple[int, int]:
    """Stamp resolution_tier + delivered_edge on each item with a locatable master. Returns
    (tagged, missing). `cache` memoizes edge-by-hash so a shared master is decoded once."""
    tagged = missing = 0
    for it in items:
        su = it.get("source_url")
        if not isinstance(su, str) or not su:
            missing += 1
            continue
        h = _hash8(su)
        if h not in cache:
            p = masters.get(h)
            if p is None:
                missing += 1
                continue
            cache[h] = _edge(p)
        edge = cache[h]
        it["delivered_edge"] = edge
        it["resolution_tier"] = tier_for(edge)
        tagged += 1
    return tagged, missing


def _collection_min_tier(items: list[dict]) -> str | None:
    tiers = [it["resolution_tier"] for it in items if it.get("resolution_tier")]
    return min(tiers, key=lambda t: _TIER_ORDER[t]) if tiers else None


def process_dir(base: Path, masters: dict[str, Path], cache: dict[str, int], write: bool) -> dict:
    """Tag every collection file in a catalog dir. Returns {collection_id: min_tier} for index roll-up."""
    mins: dict[str, str] = {}
    hist: dict[str, int] = {"HD": 0, "4K": 0, "8K": 0}
    total_missing = 0
    for path in sorted(base.glob("*.json")):
        if path.name == "index.json" or path.name.startswith("_"):
            continue
        doc = json.loads(path.read_text())
        items = doc if isinstance(doc, list) else doc.get("items", [])
        tagged, missing = tag_items(items, masters, cache)
        total_missing += missing
        cid = path.stem
        mt = _collection_min_tier(items)
        if mt:
            mins[cid] = mt
        for it in items:
            t = it.get("resolution_tier")
            if t:
                hist[t] += 1
        if write and tagged:
            path.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    print(f"  {base}: HD {hist['HD']} · 4K {hist['4K']} · 8K {hist['8K']}"
          + (f"  ({total_missing} without a locatable master — left untagged)" if total_missing else ""))
    return mins


def write_index_min_tiers(index_path: Path, mins: dict[str, str], write: bool) -> None:
    """Stamp each collection summary in index.json with its `min_tier` (the pack-card '4K+' roll-up)."""
    if not index_path.exists():
        return
    idx = json.loads(index_path.read_text())
    changed = 0
    for c in idx.get("collections", []):
        mt = mins.get(c.get("id"))
        if mt and c.get("min_tier") != mt:
            c["min_tier"] = mt
            changed += 1
    if write and changed:
        index_path.write_text(json.dumps(idx, indent=1, ensure_ascii=False))
    print(f"  {index_path}: {changed} collection min_tier(s) {'written' if write else 'would change'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Tag catalog works with a resolution tier (see module docstring).")
    ap.add_argument("--dir", action="append", dest="dirs", help=f"catalog dir(s). Default: {DEFAULT_DIRS}")
    ap.add_argument("--pack", type=Path, default=Path(DEFAULT_PACK), help="built pack dir (has _Library/)")
    ap.add_argument("--write", action="store_true", help="apply (default: dry-run)")
    args = ap.parse_args()

    library = args.pack / "_Library"
    if not library.is_dir():
        print(f"FAIL: no masters at {library} — run tools.build_pack first.")
        return 1
    masters = index_masters(library)
    print(f"indexed {len(masters)} masters under {library}")

    cache: dict[str, int] = {}
    all_mins: dict[str, str] = {}
    for d in (args.dirs or DEFAULT_DIRS):
        base = Path(d)
        if not base.is_dir():
            print(f"  (skip: {d} not found)")
            continue
        mins = process_dir(base, masters, cache, args.write)
        all_mins.update(mins)  # served catalog processed first; its ids win for the index
        write_index_min_tiers(base / "index.json", mins, args.write)

    print(f"\n{'wrote' if args.write else 'would write'} tiers"
          + ("" if args.write else "  (dry-run — re-run with --write to apply)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
