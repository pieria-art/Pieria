"""The shipped catalog must not point at AIC's Cloudflare-walled full/max endpoint, and index/pending
bookkeeping must stay honest.

AIC's IIIF host 403s the `full/max` request for server-side clients (Cloudflare), BUT bounded sizes
(`full/<N>,`) and deep-zoom region tiles pass unblocked (CURATION-v2 finding — see memory
aic-iiif-3000px-cap). So CURATION-v2 re-sources AIC works to `full/2000` (serves live + clears
verify_sources' 2000px gate) and build_pack stitches the native master from tiles for the pack. The ban
therefore narrows: no `full/max` AIC URL may ship (it would 403 live), but bounded AIC URLs are allowed.
Index counts must match reality, and every parked item must retain the accession needed to hand-source it.
"""
import json
from pathlib import Path

CATALOG_DIR = Path(__file__).resolve().parent.parent / "static" / "catalog"
PENDING = CATALOG_DIR / "_pending_artic.json"


def _collection_files():
    return [f for f in sorted(CATALOG_DIR.glob("*.json"))
            if f.name not in ("index.json", PENDING.name)]


def test_no_artic_full_max_in_shipped_catalog():
    """Bounded AIC URLs (full/<N>,) resolve and are allowed; only full/max 403s and must not ship."""
    bad = []
    for f in _collection_files():
        for it in json.loads(f.read_text()).get("items", []):
            for key in ("source_url", "thumbnail_url"):
                u = it.get(key, "")
                if "artic.edu" in u and "/full/max/" in u:
                    bad.append(f"{f.name}: {it.get('title')}")
    assert bad == [], f"shipped catalog references the Cloudflare-walled artic full/max endpoint: {bad}"


def test_index_has_no_artic_full_max_cover_thumbnails():
    index = json.loads((CATALOG_DIR / "index.json").read_text())
    bad = [c["id"] for c in index["collections"]
           if "artic.edu" in c.get("cover_thumbnail", "") and "/full/max/" in c.get("cover_thumbnail", "")]
    assert bad == [], f"index cover_thumbnail points at artic full/max: {bad}"


def test_index_counts_match_collection_files():
    index = json.loads((CATALOG_DIR / "index.json").read_text())
    for coll in index["collections"]:
        cfile = CATALOG_DIR / f"{coll['id']}.json"
        if cfile.exists():
            actual = len(json.loads(cfile.read_text()).get("items", []))
            assert coll["count"] == actual, f"{coll['id']}: index says {coll['count']}, file has {actual}"


def test_parked_items_retain_handsourcing_metadata():
    if not PENDING.exists():
        return  # nothing parked is a valid state
    parked = json.loads(PENDING.read_text())
    assert parked, "pending file exists but is empty"
    for p in parked:
        assert p.get("_park_reason"), f"parked item missing reason: {p.get('title')}"
        assert p["_park_reason"] != "unknown", f"parked item has unknown reason: {p.get('title')}"
        # The AIC accession is the key for hand-sourcing; it should survive parking.
        assert "_aic_accession" in p, f"parked item missing accession field: {p.get('title')}"
