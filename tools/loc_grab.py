"""
tools/loc_grab.py — Library of Congress high-res harvester (maintainer build tool).

The LOC counterpart to greedy_grab.py (which harvests Wikimedia Commons categories). LOC is a
SEARCH catalog, not a category tree, so this walks a curated query set instead of a BFS. It is the
grab stage of the ADR-040 grab → Sonnet-clean → inject pipeline; the output is a *candidates* file
the Sonnet clean stage turns into placard-grade items (see clean_inject.py for the inject stage).

Built for the "Cities & Architecture" collection (catalog_spec: sources=["loc"], kind=photo) — the
Belle Époque Detroit Publishing / Photochrom prints of world cities, plus B&W architectural views.

    python -m tools.loc_grab --collection photochrom-prints --out scratch/loc_cities.json
    python -m tools.loc_grab --search --queries-file q.txt -o out.json   # /search/ instead of a collection

WHY A MASTER-TIFF UPGRADE (the whole reason this tool exists):
LOC's search exposes only small service JPEGs (max 1024px) — far below the ADR-039 3840 pack floor
(the same trap that made AIC hard). The full-resolution image lives ONLY as a master TIFF under
storage-services/master/…u.tif (typically 5000–8000px). We DERIVE that master url from the service
JPEG the search already returns (/service/…Nv.jpg -> /master/…Nu.tif — a stable LOC storage
convention), then read its exact pixel dims via two ranged GETs (the TIFF IFD sits at the *end* of
these ~100MB masters, so a front-read can't see it, but storage-services honours HTTP Range — 16
bytes for the IFD offset + a few KB for the width/height tags).

CLOUDFLARE NOTE: www.loc.gov (search) sits behind Cloudflare and rate-limits an aggressive crawl with
a "Just a moment…" 429. tile.loc.gov (the image storage where the masters + dim-probes live) does NOT.
So this tool hits www.loc.gov ONCE PER QUERY (never per item) and does all per-item work against
tile.loc.gov — keeping the challenged host's load minimal. Run with --throttle 3+ to stay polite.

source_url = the master TIFF (build_pack fetches + downscales it; the browser never renders it
directly — the server's Pillow always mediates). thumbnail_url = the 1024px service JPEG for the
browse grid. LOC photochroms are PD ("no known restrictions"); audit_licenses blesses source=LOC by
policy.
"""

import argparse
import json
import re
import struct
import sys
import time
from pathlib import Path

import httpx

UA = "Pieria/1.0 (offline art catalog build; jmyost@gmail.com)"

MIN_NATIVE_EDGE = 3840  # true-4K floor (ADR-039): pack-survivable long edge
MAX_ASPECT = 4.0        # reject ultra-panoramic strips (matches greedy_grab)

# LoC search mixes digitized items with essays, guides, and landing pages. These title phrases mark
# the non-artwork "web page" records to drop (mirrors sources._LOC_JUNK_TITLE, kept local so the tool
# stands alone behind .dockerignore).
_JUNK_TITLE = re.compile(
    r"articles and essays|collection highlights|finding images|interview with|about this collection|"
    r"virtual orientation|free to use and reuse|do the talking|webcast|web guide|technical information|"
    r"related resources|rights and access|^\[.*photochrom prints of .*\]$",
    re.I,
)

# Default photochrom query set — world + US cities and architectural themes with strong Detroit
# Publishing / Photochrom coverage. Each query is run against the collection endpoint (scoped, so
# results are all photochrom prints — no keyword cross-contamination).
DEFAULT_QUERIES = [
    # US
    "New York", "Chicago", "Boston", "Washington", "San Francisco", "New Orleans",
    "Philadelphia", "Niagara Falls", "Atlantic City", "St. Augustine", "Baltimore", "Detroit",
    # Europe
    "Paris", "London", "Venice", "Rome", "Naples", "Florence", "Vienna", "Amsterdam",
    "Cologne", "Berlin", "Geneva", "Lucerne", "Stockholm", "Moscow", "Constantinople",
    "Athens", "Granada", "Seville", "Prague", "Budapest", "Edinburgh", "Dublin",
    # Middle East / world
    "Cairo", "Jerusalem", "Damascus", "Algiers",
    # Architectural themes
    "cathedral", "castle", "harbor", "bridge", "boulevard", "canal", "market", "tower",
]

_MIN_INTERVAL = [1.2]  # polite LOC pacing (mutable; --throttle overrides — LOC rate-limits the
                       # 3-requests-per-item pattern, so the real harvest runs slower)
_last = [0.0]


def _throttle():
    wait = _MIN_INTERVAL[0] - (time.monotonic() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.monotonic()


def _get_json(client: httpx.Client, url: str, params: dict) -> dict | None:
    """One LOC read with throttle + light retry on transient transport/5xx/429 errors."""
    last_exc = None
    for attempt in range(4):
        _throttle()
        try:
            r = client.get(url, params={**params, "fo": "json"}, timeout=45.0)
            if r.status_code in (429,) or r.status_code >= 500:
                raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
            if r.status_code != 200:
                return None
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError, json.JSONDecodeError) as e:
            last_exc = e
            time.sleep(2.0 * (attempt + 1))  # backoff — a transient blip must not lose the query
    print(f"  ⚠ giving up on {url} ({last_exc})", file=sys.stderr)
    return None


def _search(client: httpx.Client, base: str, query: str, count: int) -> list[dict]:
    """Return raw result records (id + title + image_url) for one query."""
    data = _get_json(client, base, {"q": query, "c": count, "at": "results"})
    if not data:
        return []
    return data.get("results", []) or []


_MASTER_RE = re.compile(r"v\.jpe?g$", re.I)


def _service_and_master(image_urls: list[str]) -> tuple[str | None, str | None]:
    """From a search result's image_url list, return (best_service_jpeg, derived_master_tiff).

    The search returns tile.loc.gov service JPEGs (…Nv.jpg, up to 1024px) with a #w=… size fragment.
    Pick the largest, then derive the full-res master by the stable LOC storage convention
    (/service/ -> /master/, the trailing …Nv.jpg -> …Nu.tif). Returns (None, None) if no service
    JPEG is present or the master can't be derived (caller skips — a non-standard asset layout)."""
    best_w, best = -1, None
    for u in image_urls or []:
        base = u.split("#", 1)[0]
        if "storage-services/service" not in base or not base.lower().endswith((".jpg", ".jpeg")):
            continue
        m = re.search(r"[?&#]w=(\d+)", u)
        w = int(m.group(1)) if m else 0
        if w >= best_w:
            best_w, best = w, base
    if not best:
        return None, None
    master = best.replace("/service/", "/master/")
    master = _MASTER_RE.sub("u.tif", master)
    if "/master/" not in master or not master.endswith("u.tif"):
        return best, None
    return best, master


def _tiff_dims(client: httpx.Client, url: str) -> tuple[int, int]:
    """Exact (width, height) of a master TIFF via two ranged GETs — no full download.

    Reads the 8-byte TIFF header for byte-order + first-IFD offset, then the IFD (which on LOC masters
    sits at the file's end) for the ImageWidth (0x0100) / ImageLength (0x0101) tags. Returns (0, 0) on
    any parse failure so the caller drops the candidate rather than admit an unknown-size image."""
    try:
        _throttle()
        h = client.get(url, headers={"Range": "bytes=0-15"}, timeout=30.0)
        b = h.content
        if b[:2] not in (b"II", b"MM"):
            return 0, 0
        order = "<" if b[:2] == b"II" else ">"
        ifd_off = struct.unpack(order + "I", b[4:8])[0]
        _throttle()
        r = client.get(url, headers={"Range": f"bytes={ifd_off}-{ifd_off + 4095}"}, timeout=30.0)
        d = r.content
        n = struct.unpack(order + "H", d[:2])[0]
        w = ht = 0
        for i in range(n):
            e = d[2 + i * 12: 2 + i * 12 + 12]
            if len(e) < 12:
                break
            tag = struct.unpack(order + "H", e[:2])[0]
            typ = struct.unpack(order + "H", e[2:4])[0]
            # ImageWidth/Length may be TIFF type 3 (SHORT, 2 bytes) or 4 (LONG, 4 bytes). A SHORT sits
            # in the FIRST 2 bytes of the value field — on a big-endian (MM) file, reading those 4
            # bytes as a LONG left-shifts the value by 16 (the ppmsc masters' 3456 -> 226492416 bug).
            val = struct.unpack(order + "H", e[8:10])[0] if typ == 3 else struct.unpack(order + "I", e[8:12])[0]
            if tag == 0x0100:
                w = val
            elif tag == 0x0101:
                ht = val
        # Sanity backstop: no real LOC master exceeds ~80k px on an edge — a larger value is a parse
        # artifact, so report 0 (the caller drops it) rather than admit a bogus dimension.
        if max(w, ht) > 100000:
            return 0, 0
        return w, ht
    except Exception as e:
        print(f"    ⚠ dims failed ({url[-40:]}): {e}", file=sys.stderr)
        return 0, 0


def _first(v):
    """LOC item fields are often single-element lists; flatten to a clean string."""
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v or "")


def grab(*, base: str, queries: list[str], per_query: int, min_edge: int, max_items: int,
         checkpoint: Path | None = None) -> list:
    kept, seen = [], set()
    stats = {"hits": 0, "junk": 0, "no_master": 0, "small": 0, "aspect": 0, "dup": 0}
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        # 1) SEARCH phase — the ONLY www.loc.gov (Cloudflare-fronted) load: one request per query.
        #    Everything the resolve phase needs (title, date, master url) comes straight from here.
        pending = []
        for q in queries:
            results = _search(client, base, q, per_query)
            print(f"query {q!r} → {len(results)} results", file=sys.stderr)
            for res in results:
                title = res.get("title") or "Untitled"
                if _JUNK_TITLE.search(title):
                    stats["junk"] += 1
                    continue
                thumb, master = _service_and_master(res.get("image_url") or [])
                if not master:
                    stats["no_master"] += 1
                    continue
                if master in seen:
                    stats["dup"] += 1
                    continue
                seen.add(master)
                pending.append({
                    "item_id": res.get("id") or "",
                    "source_url": master,
                    "thumbnail_url": thumb,
                    "raw_title": title,
                    "raw_date": _first(res.get("date")),
                    "location": res.get("location") or [],
                    "subjects": res.get("subject") or [],
                    "query": q,
                })
                if len(pending) >= max_items:
                    break
            if len(pending) >= max_items:
                break

        print(f"\nresolving {len(pending)} unique masters (tile.loc.gov dim-probe)…", file=sys.stderr)
        # 2) RESOLVE phase — all against tile.loc.gov (NOT Cloudflare-challenged): probe master dims,
        #    gate at source. No www.loc.gov calls here.
        for stub in pending:
            stats["hits"] += 1
            w, h = _tiff_dims(client, stub["source_url"])
            if max(w, h) < min_edge:
                stats["small"] += 1
                print(f"    small {w}x{h}: {stub['raw_title'][:44]}", file=sys.stderr)
                continue
            lo, hi = sorted((w, h))
            if lo == 0 or hi / lo > MAX_ASPECT:
                stats["aspect"] += 1
                continue
            kept.append({
                **stub,
                "width": w, "height": h,
                "medium": "Photochrom print",
                "source": "Library of Congress",
            })
            print(f"    ✓ {w}x{h}  {stub['raw_title'][:50]}", file=sys.stderr)
            # Checkpoint after every keep so a mid-run LOC rate-limit stall never loses the harvest.
            if checkpoint is not None:
                checkpoint.write_text(json.dumps(kept, indent=1, ensure_ascii=False))

    kept.sort(key=lambda x: x["raw_title"])
    print(f"\nKEPT {len(kept)} / {stats['hits']} resolved  "
          f"(junk {stats['junk']}, no-master {stats['no_master']}, small {stats['small']}, "
          f"aspect {stats['aspect']})", file=sys.stderr)
    return kept


def main():
    ap = argparse.ArgumentParser(description="Library of Congress high-res harvester (ADR-040 grab stage).")
    ap.add_argument("-o", "--out", required=True, help="output candidates JSON path")
    ap.add_argument("--collection", default="photochrom-prints",
                    help="LOC collection slug to scope the search (default photochrom-prints)")
    ap.add_argument("--search", action="store_true",
                    help="use the global /search/ endpoint instead of a collection (for B&W supplements)")
    ap.add_argument("--queries-file", help="newline-separated query list (default: built-in photochrom set)")
    ap.add_argument("--per-query", type=int, default=25, help="results per query (default 25)")
    ap.add_argument("--min-edge", type=int, default=MIN_NATIVE_EDGE, help=f"native long-edge floor (default {MIN_NATIVE_EDGE})")
    ap.add_argument("--max-items", type=int, default=300, help="cap on unique items to resolve (default 300)")
    ap.add_argument("--throttle", type=float, default=1.2,
                    help="seconds between LOC requests (default 1.2; raise to ~2.5 to avoid rate-limiting)")
    args = ap.parse_args()

    _MIN_INTERVAL[0] = args.throttle

    if args.queries_file:
        queries = [ln.strip() for ln in Path(args.queries_file).read_text().splitlines() if ln.strip()]
    else:
        queries = DEFAULT_QUERIES

    base = "https://www.loc.gov/search/" if args.search else \
        f"https://www.loc.gov/collections/{args.collection}/"
    print(f"base: {base}  ({len(queries)} queries)", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    kept = grab(base=base, queries=queries, per_query=args.per_query,
                min_edge=args.min_edge, max_items=args.max_items, checkpoint=out)
    out.write_text(json.dumps(kept, indent=1, ensure_ascii=False))
    print(f"→ wrote {len(kept)} candidates to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
