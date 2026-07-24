"""Re-source Art-Institute-of-Chicago catalog items onto Wikimedia Commons (rot-proof Special:FilePath).

WHY: AIC's IIIF image host (www.artic.edu/iiif/2/) now sits behind a zone-wide Cloudflare managed
challenge (`cf-mitigated: challenge`) that 403s every non-browser client — see memory catalog-host-rot.
The works are public-domain (CC0) and most are mirrored on Commons, which we already fetch reliably.
We honor AIC's bot wall rather than bulldoze it, and substitute the Commons image.

PRECISION over fuzzing: the catalog's IIIF URLs carry AIC `image_id`s; AIC's open *data* API (which is
NOT challenged) resolves image_id -> accession number (`main_reference_number`); Wikidata's inventory
property P217 maps that exact accession to its Commons image (P18). That identity match never confuses
"Netting the Fish" with a PDF the way a title search does. A clean unique title+artist match (also via
Wikidata, AIC-collection-tagged) is the secondary trustworthy tier. Anything weaker is PARKED — moved
out of the shipped catalog into static/catalog/_pending_artic.json (fully restorable), not deleted,
for Josh's email-to-AIC + later hand-sourcing.

Gracious-consumer: AIC bulk lookups are ~4 paged POSTs (2s apart); Wikidata is one VALUES query;
Commons verification is throttled. All results cache to tools/.artic_cache.json (gitignored) so reruns
are instant. Network stages re-run only with --refresh.

    python -m tools.resource_artic                 # DRY RUN: resolve, match, verify, print the plan
    python -m tools.resource_artic --plan out.json # also dump the full plan for eyeballing
    python -m tools.resource_artic --apply         # mutate static/catalog/*.json + park the tail
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote

from scout import MIN_DISPLAY_EDGE, _wikimedia_filepath, _wm_match  # URL builder, scorer, res gate

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "static" / "catalog"
PENDING_FILE = CATALOG_DIR / "_pending_artic.json"
CACHE_FILE = Path(__file__).resolve().parent / ".artic_cache.json"

AIC_QID = "Q239303"  # Art Institute of Chicago (Wikidata)
AIC_SEARCH = "https://api.artic.edu/api/v1/artworks/search"
SPARQL = "https://query.wikidata.org/sparql"
UA = "Pieria/1.0 (https://github.com/pieria-art/Pieria; jmyost@gmail.com) re-source"
AIC_UA = "Pieria (jmyost@gmail.com)"  # AIC asks clients to set this

SOURCE_WIDTH = 3840  # matches the live resolver + seed convention (scout.py:864)
THUMB_WIDTH = 600
AIC_PAGE = 100
AIC_DELAY = 2.0     # gracious: AIC allows 60/min (1s); we do 1 per 2s
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WM_BATCH = 50       # imageinfo accepts up to 50 titles/call — verify metadata, never render
WM_DELAY = 1.5      # between batches; honors Retry-After/maxlag on top
TITLE_MATCH_MIN = 0.60  # _wm_match threshold for the title-fallback tier

# ---------------------------------------------------------------- catalog I/O


def collect_artic_items():
    """Every artic-sourced item, tagged with its file + index so --apply can rewrite in place."""
    out = []
    for f in sorted(CATALOG_DIR.glob("*.json")):
        if f.name in ("index.json", PENDING_FILE.name):
            continue
        data = json.loads(f.read_text())
        for idx, it in enumerate(data.get("items", [])):
            src = it.get("source_url", "")
            if "artic.edu" not in src:
                continue
            image_id = _image_id(src)
            out.append({
                "file": f.name, "collection": data.get("id", f.stem), "idx": idx,
                "title": it.get("title", ""), "artist": it.get("agent_name", ""),
                "image_id": image_id, "source_url": src,
            })
    return out


def _image_id(iiif_url: str) -> str:
    # https://www.artic.edu/iiif/2/<image_id>/full/max/0/default.jpg
    parts = iiif_url.split("/iiif/2/", 1)
    return parts[1].split("/", 1)[0] if len(parts) == 2 else ""


# ---------------------------------------------------------------- cache


def load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {"accessions": {}, "wikidata_rows": None, "verified": {}}


def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- stage A: AIC accession lookup


def resolve_accessions(image_ids, cache, refresh=False):
    """image_id -> {accession, title, artist} via AIC's (un-challenged) data API. Paged + gentle."""
    have = cache["accessions"]
    todo = [i for i in image_ids if i and (refresh or i not in have)]
    if todo:
        print(f"[AIC] resolving {len(todo)} image_ids -> accession ({len(todo)//AIC_PAGE + 1} pages, "
              f"{AIC_DELAY}s apart)...", file=sys.stderr)
    for p in range(0, len(todo), AIC_PAGE):
        page = todo[p:p + AIC_PAGE]
        body = json.dumps({
            "query": {"terms": {"image_id": page}},
            "fields": ["image_id", "title", "main_reference_number", "artist_title"],
            "limit": AIC_PAGE,
        }).encode()
        req = urllib.request.Request(AIC_SEARCH, data=body, method="POST", headers={
            "Content-Type": "application/json", "AIC-User-Agent": AIC_UA, "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=45) as resp:  # noqa: S310 (trusted, fixed host)
            data = json.load(resp)["data"]
        for row in data:
            have[row["image_id"]] = {
                "accession": row.get("main_reference_number", ""),
                "title": row.get("title", ""), "artist": row.get("artist_title", ""),
            }
        save_cache(cache)
        if p + AIC_PAGE < len(todo):
            time.sleep(AIC_DELAY)
    return have


# ---------------------------------------------------------------- stage B: Wikidata index


def fetch_wikidata_rows(cache, refresh=False):
    """All AIC-collection works with a Commons image; indexed later by accession AND by title."""
    if cache["wikidata_rows"] is not None and not refresh:
        return cache["wikidata_rows"]
    query = f"""
SELECT ?item ?itemLabel ?creatorLabel ?inv ?image WHERE {{
  ?item wdt:P195 wd:{AIC_QID} .
  ?item wdt:P18 ?image .
  OPTIONAL {{ ?item wdt:P217 ?inv . }}
  OPTIONAL {{ ?item wdt:P170 ?creator . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}"""
    print("[Wikidata] fetching AIC works-with-image...", file=sys.stderr)
    url = f"{SPARQL}?{urllib.parse.urlencode({'query': query, 'format': 'json'})}"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310 (trusted, fixed host)
        bindings = json.load(resp)["results"]["bindings"]
    rows = [{
        "qid": b["item"]["value"].rsplit("/", 1)[-1],
        "label": b.get("itemLabel", {}).get("value", ""),
        "creator": b.get("creatorLabel", {}).get("value", ""),
        "inv": b.get("inv", {}).get("value", ""),
        "image": b["image"]["value"],
    } for b in bindings]
    cache["wikidata_rows"] = rows
    save_cache(cache)
    return rows


def _commons_filename(p18_url: str) -> str:
    # http://commons.wikimedia.org/wiki/Special:FilePath/<Filename>
    return unquote(p18_url.split("Special:FilePath/", 1)[-1])


# ---------------------------------------------------------------- stage C: matching


def build_indexes(rows):
    by_inv, by_title = {}, defaultdict(list)
    for r in rows:
        if r["inv"]:
            by_inv.setdefault(r["inv"].strip(), r)  # first wins; accession is ~unique within AIC
        by_title[_norm(r["label"])].append(r)
    return by_inv, by_title


def _norm(s: str) -> str:
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    for ch in "—–-":
        s = s.replace(ch, " ")
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    s = re.sub(r"^(the|a|an)\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def choose_match(item, accessions, by_inv, by_title):
    """Return (commons_filename, qid, confidence, reason) or None. Two trustworthy tiers only."""
    acc = (accessions.get(item["image_id"]) or {}).get("accession", "")
    # Tier 1 — exact accession identity.
    if acc and acc.strip() in by_inv:
        r = by_inv[acc.strip()]
        return _commons_filename(r["image"]), r["qid"], "accession", acc
    # Tier 2 — title match ONLY when the artist genuinely agrees (no fallback: "Woman at Her
    # Toilette" exists for both Degas and Morisot, so a title-only accept is a false identity).
    cands = by_title.get(_norm(item["title"]), [])
    surname = (item["artist"].split()[-1] if item["artist"] else "").lower()
    narrowed = [c for c in cands if len(surname) > 2 and surname in (c["creator"] or "").lower()]
    if len(narrowed) == 1:
        r = narrowed[0]
        fname = _commons_filename(r["image"])
        if _wm_match(fname, item["title"], item["artist"]) >= TITLE_MATCH_MIN:
            return fname, r["qid"], "title", None
    return None


# ---------------------------------------------------------------- stage D: verify (batched, no render)


def _norm_fname(name: str) -> str:
    """Commons titles are case-insensitive on the first char and treat _ == space."""
    name = name.replace("_", " ").strip()
    return (name[:1].upper() + name[1:]) if name else name


def _commons_imageinfo(filenames):
    """One imageinfo call for up to 50 files -> {filename: {mime,w,h}}. Honors maxlag/429 backoff."""
    titles = "|".join(f"File:{f}" for f in filenames)
    params = {"action": "query", "format": "json", "prop": "imageinfo",
              "iiprop": "mime|size", "maxlag": "5", "titles": titles}
    url = f"{COMMONS_API}?{urllib.parse.urlencode(params)}"
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as resp:  # noqa: S310 (trusted, fixed host)
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            wait = int(e.headers.get("Retry-After", 5)) if e.code == 429 else 2 ** attempt
            print(f"  [commons] {e.code}; backing off {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if "error" in data and data["error"].get("code") == "maxlag":
            time.sleep(5)
            continue
        out, want = {}, {_norm_fname(f): f for f in filenames}
        for page in data.get("query", {}).get("pages", {}).values():
            orig = want.get(_norm_fname(page.get("title", "").split(":", 1)[-1]))
            if orig is None:
                continue
            if "missing" in page or not page.get("imageinfo"):
                out[orig] = None
            else:
                ii = page["imageinfo"][0]
                out[orig] = {"mime": ii.get("mime", ""), "w": ii.get("width", 0), "h": ii.get("height", 0)}
        return out
    return {f: None for f in filenames}


def verify_commons(filenames, cache, refresh=False):
    """Batch-verify candidate files via cheap metadata: real raster image + adequate resolution."""
    info = cache.setdefault("imageinfo", {})
    todo = sorted({f for f in filenames if refresh or f not in info})
    if todo:
        print(f"[commons] imageinfo for {len(todo)} files "
              f"({(len(todo) + WM_BATCH - 1)//WM_BATCH} batches of {WM_BATCH})...", file=sys.stderr)
    for b in range(0, len(todo), WM_BATCH):
        info.update(_commons_imageinfo(todo[b:b + WM_BATCH]))
        save_cache(cache)
        if b + WM_BATCH < len(todo):
            time.sleep(WM_DELAY)

    def verdict(fname):
        meta = info.get(fname)
        if not meta:
            return False, "missing"
        if not meta["mime"].startswith("image/") or "svg" in meta["mime"]:
            return False, f"not-raster({meta['mime']})"
        if max(meta["w"], meta["h"]) < MIN_DISPLAY_EDGE:
            return False, f"too-small({meta['w']}x{meta['h']})"
        return True, "ok"

    return {f: verdict(f) for f in filenames}


# ---------------------------------------------------------------- plan + apply


def build_plan(refresh=False, limit=None):
    cache = load_cache()
    items = collect_artic_items()
    if limit:
        items = items[:limit]
    accessions = resolve_accessions([i["image_id"] for i in items], cache, refresh)
    rows = fetch_wikidata_rows(cache, refresh)
    by_inv, by_title = build_indexes(rows)
    print(f"[Wikidata] {len(rows)} works ({len(by_inv)} with accession)\n", file=sys.stderr)

    # Phase 1 — match every item (no network); collect candidate files for one batched verify.
    candidates, parked = [], []
    for it in items:
        m = choose_match(it, accessions, by_inv, by_title)
        if not m:
            parked.append({**it, "reason": "no-trustworthy-match",
                           "accession": (accessions.get(it["image_id"]) or {}).get("accession", "")})
        else:
            fname, qid, confidence, acc = m
            candidates.append({**it, "confidence": confidence, "qid": qid,
                               "accession": acc, "commons_file": fname})

    # Phase 2 — verify all candidate files at once (cheap metadata, never a render).
    verdicts = verify_commons([c["commons_file"] for c in candidates], cache, refresh)
    matched = []
    for c in candidates:
        ok, why = verdicts[c["commons_file"]]
        if ok:
            matched.append({**c,
                            "new_source": _wikimedia_filepath(c["commons_file"], SOURCE_WIDTH),
                            "new_thumb": _wikimedia_filepath(c["commons_file"], THUMB_WIDTH)})
        else:
            parked.append({**c, "reason": f"verify-failed:{why}"})
    return items, matched, parked


def apply_plan(matched, parked):
    """Rewrite matched items in place; move parked items into _pending_artic.json; fix index.json."""
    by_file = defaultdict(lambda: {"rewrite": {}, "remove": set()})
    for m in matched:
        by_file[m["file"]]["rewrite"][m["idx"]] = (m["new_source"], m["new_thumb"])
    park_meta = {(p["file"], p["idx"]): p for p in parked}
    for p in parked:
        by_file[p["file"]]["remove"].add(p["idx"])

    pending_out = []
    cover_fix = {}  # collection id -> a surviving thumbnail_url
    for fname, ops in by_file.items():
        path = CATALOG_DIR / fname
        data = json.loads(path.read_text())
        items = data.get("items", [])
        for idx, (src, thumb) in ops["rewrite"].items():
            items[idx]["source_url"] = src
            items[idx]["thumbnail_url"] = thumb
        for idx in ops["remove"]:
            meta = park_meta.get((fname, idx), {})
            pending_out.append({**items[idx],
                                "_parked_from": data.get("id", path.stem),
                                "_park_reason": meta.get("reason", "unknown"),
                                "_aic_accession": meta.get("accession", ""),
                                "_soft_commons_file": meta.get("commons_file", "")})
        kept = [it for i, it in enumerate(items) if i not in ops["remove"]]
        data["items"] = kept
        if kept:
            cover_fix[data.get("id", path.stem)] = kept[0].get("thumbnail_url", "")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1))  # match generator: no trailing \n

    if pending_out:
        existing = json.loads(PENDING_FILE.read_text()) if PENDING_FILE.exists() else []
        PENDING_FILE.write_text(json.dumps(existing + pending_out, ensure_ascii=False, indent=1) + "\n")

    # index.json: refresh per-collection count + repair any artic cover_thumbnail.
    index_path = CATALOG_DIR / "index.json"
    index = json.loads(index_path.read_text())
    for coll in index.get("collections", []):
        cid = coll.get("id")
        cfile = CATALOG_DIR / f"{cid}.json"
        if cfile.exists():
            coll["count"] = len(json.loads(cfile.read_text()).get("items", []))
        if "artic.edu" in coll.get("cover_thumbnail", "") and cover_fix.get(cid):
            coll["cover_thumbnail"] = cover_fix[cid]
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1))  # match generator: no trailing \n
    return len(pending_out)


# ---------------------------------------------------------------- CLI


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="mutate the catalog (default is dry-run)")
    ap.add_argument("--plan", metavar="PATH", help="dump the full plan JSON for eyeballing")
    ap.add_argument("--refresh", action="store_true", help="ignore cached network results")
    ap.add_argument("--limit", type=int, help="process only the first N items (testing)")
    args = ap.parse_args()

    items, matched, parked = build_plan(refresh=args.refresh, limit=args.limit)
    total = len(items)
    acc = sum(1 for m in matched if m["confidence"] == "accession")
    ttl = sum(1 for m in matched if m["confidence"] == "title")
    reasons = defaultdict(int)
    for p in parked:
        reasons[p["reason"]] += 1

    print("=" * 64)
    print(f"  artic items:              {total}")
    print(f"  MATCHED + verified:       {len(matched)}  ({len(matched)/total:.0%})")
    print(f"      via accession (exact):  {acc}")
    print(f"      via title+artist:       {ttl}")
    print(f"  PARKED (kept, recoverable): {len(parked)}")
    for r, n in sorted(reasons.items()):
        print(f"      {r}: {n}")
    print("=" * 64)
    for m in matched[:6]:
        print(f"  ✓ [{m['confidence']:9}] {m['artist']} — {m['title']!r}\n        → {m['commons_file']}")

    if args.plan:
        Path(args.plan).write_text(json.dumps(
            {"matched": matched, "parked": parked}, ensure_ascii=False, indent=2))
        print(f"\nplan written to {args.plan}", file=sys.stderr)

    if args.apply:
        n_parked = apply_plan(matched, parked)
        print(f"\nAPPLIED: rewrote {len(matched)} items; parked {n_parked} -> {PENDING_FILE.name}")
    else:
        print("\n(dry run — no files changed; re-run with --apply to commit)")


if __name__ == "__main__":
    main()
