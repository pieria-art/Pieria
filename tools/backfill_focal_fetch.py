"""Fetch catalog/seed thumbnails to local disk so a Claude Code agent fan-out can derive focal points
by *looking* at each image (Read renders it), then bake the results with backfill_focal_bake.py.
This keeps the bulk vision pass in-IDE (no app API key needed) — see ROADMAP increment ⑦.

Usage (from repo root, with the venv):
  venv/bin/python -m tools.backfill_focal_fetch --collection portraits   # one collection (pilot)
  venv/bin/python -m tools.backfill_focal_fetch --all                    # all 24 served collections
  venv/bin/python -m tools.backfill_focal_fetch --seed                   # factory_seed.json (25 items)

Writes images to <out>/imgs/<key>.jpg and a manifest to <out>/manifest.json (key = sha1(source_url)
[:16] — the join key for baking). out = /tmp/sd_focal (catalog) or /tmp/sd_focal_seed (seed). Skips
_pending_artic.json (Cloudflare-walled) and any item that already carries focal_point. Incremental:
an image already on disk is not re-fetched.
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import httpx

CAT = Path("static/catalog")
SEED = Path("static/factory_seed.json")
UA = "Pieria-FocalBackfill/1.0 (https://github.com/AiwendilInTheWoods/Pieria)"
SKIP = {"index.json", "_pending_artic.json"}


def key_of(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def items_of(path: Path):
    """(label, items) for a catalog collection file or the seed list."""
    d = json.loads(path.read_text())
    items = d if isinstance(d, list) else d.get("items", [])
    return path.stem, items


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--collection")
    g.add_argument("--all", action="store_true")
    g.add_argument("--seed", action="store_true")
    ap.add_argument("--delay", type=float, default=0.4, help="polite delay between fetches (s)")
    args = ap.parse_args()

    if args.seed:
        files, out = [SEED], Path("/tmp/sd_focal_seed")
    else:
        files = sorted(f for f in CAT.glob("*.json") if f.name not in SKIP)
        if not args.all:
            files = [f for f in files if f.stem == args.collection]
        out = Path("/tmp/sd_focal")
    if not files:
        sys.exit(f"No source files matched {args.collection!r}")
    img = out / "imgs"
    img.mkdir(parents=True, exist_ok=True)

    manifest, fetched = [], 0
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=30) as c:
        for f in files:
            label, items = items_of(f)
            for it in items:
                if it.get("focal_point") is not None or not it.get("source_url"):
                    continue
                url = it.get("thumbnail_url") or it["source_url"]
                k = key_of(it["source_url"])
                dest = img / f"{k}.jpg"
                entry = {"key": k, "file": str(dest), "source_url": it["source_url"],
                         "title": it.get("title", ""), "collection": label}
                if not dest.exists():
                    try:
                        r = c.get(url)
                        for _ in range(2):
                            if r.status_code != 429:
                                break
                            time.sleep(3)
                            r = c.get(url)
                        if r.status_code == 200 and r.content:
                            dest.write_bytes(r.content)
                            fetched += 1
                        else:
                            entry["error"] = f"HTTP {r.status_code}"
                    except Exception as e:  # network hiccup — record, keep going
                        entry["error"] = str(e)[:80]
                    time.sleep(args.delay)
                manifest.append(entry)

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    ok = sum(1 for m in manifest if Path(m["file"]).exists())
    errs = [m for m in manifest if m.get("error")]
    print(f"fetched {fetched} new · {ok}/{len(manifest)} thumbnails ready → {out/'manifest.json'}")
    for m in errs[:10]:
        print(f"  ✗ {m['collection']}: {m['title'][:40]} — {m['error']}")


if __name__ == "__main__":
    main()
