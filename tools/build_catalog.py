"""
Offline catalog builder (maintainer tool — NOT part of the runtime image).

Pulls public-domain art from the museum scouts + NASA + Library of Congress + curated Wikimedia
files, enriches each work once with a museum placard (baked into the static manifest as plain
text), verifies the image URLs resolve, and writes the split manifest the app serves:

    static/catalog/index.json        # collection summaries (cover + count)
    static/catalog/<collection>.json # the items

Run from the repo root:

    python -m tools.build_catalog                                  # whole curated catalog
    python -m tools.build_catalog --collection cosmos --limit 6    # one collection, capped
    python -m tools.build_catalog --no-enrich --no-verify          # fast dry pass

Enrichment is a one-time cost (uses the configured model, text-only ~cents); results are cached in
tools/.catalog_cache.json keyed by source_url, so reruns are incremental. Use --no-enrich to fall
back to a deterministic template placard (no model needed).
"""

import argparse
import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image

load_dotenv()  # pick up GEMINI_API_KEY (and any AI config) from .env, like the app does

import ai_client
from database import SessionLocal
from tools import catalog_spec
from tools.sources import (
    MIN_DISPLAY_EDGE,
    MUSEUM_SOURCES,
    UA,
    fetch_collection,
    resolve_loc,
    resolve_museums,
    resolve_nasa,
    resolve_smithsonian,
    resolve_wikimedia,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("catalog-builder")

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = REPO_ROOT / "static" / "catalog"
CACHE_FILE = REPO_ROOT / "tools" / ".catalog_cache.json"


# ---------------------------------------------------------------- cache
def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=1))


# ---------------------------------------------------------------- select
def _norm_key(item: dict) -> str:
    t = re.sub(r"[^a-z0-9]+", " ", (item.get("title") or "").lower()).strip()
    a = re.sub(r"[^a-z0-9]+", " ", (item.get("agent_name") or "").lower()).strip()
    return f"{t}|{a}"


def _hires_score(url: str) -> int:
    u = (url or "").lower()
    return sum(t in u for t in ("orig", "full/max", "2000", "/full/", "~orig"))


def dedupe_and_select(items: list, target: int) -> list:
    """Drop near-duplicates (by title+artist and by source_url), prefer higher-res sources, cap."""
    by_key, by_url = {}, set()
    for it in items:
        if not it.get("source_url") or not it.get("thumbnail_url"):
            continue
        if it["source_url"] in by_url:
            continue
        key = _norm_key(it)
        prev = by_key.get(key)
        if prev is None or _hires_score(it["source_url"]) > _hires_score(prev["source_url"]):
            if prev is not None:
                by_url.discard(prev["source_url"])
            by_key[key] = it
            by_url.add(it["source_url"])
    selected = list(by_key.values())
    # Prefer items that already have more metadata, then higher-res.
    selected.sort(key=lambda i: (bool(i.get("date_display")), _hires_score(i["source_url"])), reverse=True)
    return selected[:target]


# ---------------------------------------------------------------- enrich
def _template_placard(item: dict) -> dict:
    t, a = item.get("title", "Untitled"), item.get("agent_name", "")
    d = item.get("date_display") or item.get("creation_date") or ""
    m = item.get("medium") or ""
    s1 = t + (f" by {a}" if a and a != "Unknown Artist" else "") + (f" ({d})" if d else "") + "."
    s2 = (f"{m}. " if m else "") + f"From the collection of {item.get('source', 'a public collection')}."
    item["description_narrative"] = (s1 + " " + s2).strip()
    if not item.get("tags"):
        words = [w.lower() for w in re.split(r"[\s,]+", t) if len(w) > 3][:5]
        item["tags"] = ", ".join(words)
    return item


_ENRICH_PROMPT = """You are a museum curator writing a wall placard. Using only well-established facts about this artwork, return ONLY a JSON object with these keys:
"description_narrative": a vivid, factual 2-sentence placard blurb,
"tags": 5-8 comma-separated lowercase keywords (subject, style, mood),
"agent_role": the maker's role (e.g. "Painter", "Printmaker", "Photographer"),
"cultural_context": movement/era/region (e.g. "Impressionism, French"),
"date_display": a short display date (e.g. "1889", "c. 1665").

Artwork facts:
Title: {title}
Artist/Maker: {artist}
Date: {date}
Medium: {medium}
Source: {source}
Known context: {context}

If unsure of a specific fact, stay general rather than inventing details."""


def enrich_item(item: dict) -> dict:
    """Fill description_narrative/tags (+ refine role/context/date) via the configured model;
    fall back to a deterministic template on any failure."""
    try:
        prompt = _ENRICH_PROMPT.format(
            title=item.get("title", ""), artist=item.get("agent_name", ""),
            date=item.get("creation_date") or item.get("date_display", ""),
            medium=item.get("medium", ""), source=item.get("source", ""),
            context=item.get("cultural_context", ""))
        text = ai_client.chat("fast", [{"role": "user", "content": prompt}], json_mode=True)
        data = ai_client.parse_json(text)
        item["description_narrative"] = data.get("description_narrative") or item["description_narrative"]
        item["tags"] = data.get("tags") or item["tags"]
        for k in ("agent_role", "cultural_context", "date_display"):
            if data.get(k) and not item.get(k):
                item[k] = data[k]
        if not item.get("description_narrative"):
            _template_placard(item)
        return item
    except Exception as e:
        logger.info(f"    · enrich fell back to template ({e})")
        return _template_placard(item)


# ---------------------------------------------------------------- verify (display-true)
async def _get(client, url, **kw):
    """GET with 429-resilience. Wikimedia rate-limits the image CDN (upload.wikimedia.org) hard during
    a big build; the verify downloads aren't throttled like the search API, so without this a burst of
    429s silently drops otherwise-valid works. Retry on 429 honouring Retry-After, capped."""
    r = None
    for attempt in range(5):
        r = await client.get(url, **kw)
        if r.status_code != 429:
            return r
        wait = 0.0
        try:
            wait = float(r.headers.get("retry-after", "") or 0)
        except ValueError:
            wait = 0.0
        await asyncio.sleep(min(wait or 2.0 * (attempt + 1), 30.0))
    return r


async def _thumb_is_real_image(client, url, min_edge=350) -> bool:
    """The thumbnail must be a real raster image of decent size — kills HTML/SVG landing pages,
    icons, and tiny derivatives that scraping otherwise lets through."""
    try:
        r = await _get(client, url, timeout=30.0, follow_redirects=True)
        if r.status_code != 200:
            return False
        ct = r.headers.get("content-type", "").lower()
        if not ct.startswith("image/") or "svg" in ct:
            return False
        Image.open(BytesIO(r.content)).verify()
        w, h = Image.open(BytesIO(r.content)).size
        return max(w, h) >= min_edge
    except Exception:
        return False

async def _source_ok(client, url) -> bool:
    """The full-res source must serve a real image AND be high enough resolution for big displays
    (≥ MIN_DISPLAY_EDGE on the long edge). Wikimedia FilePath URLs are pre-gated on the original's
    size at resolve time (imageinfo), so for those a cheap content-type check suffices; everything
    else is downloaded and measured."""
    try:
        if "commons.wikimedia.org" in url:
            r = await _get(client, url, timeout=45.0, follow_redirects=True, headers={"Range": "bytes=0-4095"})
            if r.status_code not in (200, 206):
                return False
            ct = r.headers.get("content-type", "").lower()
            return ct.startswith("image/") and "svg" not in ct
        # Read just the header bytes to get dimensions — avoids downloading huge originals
        # (museum full/max files can be tens of MB). JPEG SOF / PNG IHDR live near the start.
        r = await _get(client, url, timeout=60.0, follow_redirects=True, headers={"Range": "bytes=0-262143"})
        if r.status_code not in (200, 206):
            return False
        ct = r.headers.get("content-type", "").lower()
        if not ct.startswith("image/") or "svg" in ct:
            return False
        try:
            w, h = Image.open(BytesIO(r.content)).size
        except Exception:
            # Header wasn't in the first chunk — fall back to a full fetch.
            r2 = await _get(client, url, timeout=90.0, follow_redirects=True)
            if r2.status_code != 200:
                return False
            w, h = Image.open(BytesIO(r2.content)).size
        return max(w, h) >= MIN_DISPLAY_EDGE
    except Exception:
        return False

async def verify_item(client, item) -> bool:
    if not await _thumb_is_real_image(client, item["thumbnail_url"]):
        return False
    return await _source_ok(client, item["source_url"])

# ---------------------------------------------------------------- curated picks
async def resolve_pick(db, pk, spec, client, verify=True):
    """Resolve one curated 'must-see' pick to a VERIFIED PD candidate, trying each source in order
    and falling through to the next if a candidate fails the display gates (so e.g. a flaky NASA
    asset falls back to Wikimedia instead of silently dropping the pick)."""
    title = pk["title"]
    artist = pk.get("artist", "")
    order = pk.get("sources") or spec.get("pick_sources") or ["wikimedia", "museums"]
    museum_srcs = [s for s in spec.get("sources", []) if s in MUSEUM_SOURCES] or \
        ["met", "chicago", "cleveland", "rijks", "smk"]
    for src in order:
        try:
            if src == "wikimedia":
                cand = await resolve_wikimedia(title, artist)
            elif src == "nasa":
                cand = await resolve_nasa(title)
            elif src == "loc":
                cand = await resolve_loc(title, artist)
            elif src == "smithsonian":
                cand = await resolve_smithsonian(db, title, artist)
            elif src == "museums":
                cand = await resolve_museums(db, title, artist, museum_srcs)
            else:
                cand = None
        except Exception as e:
            logger.warning(f"    resolve {src} failed for '{title}': {e}")
            cand = None
        if not cand:
            continue
        if artist and cand.get("agent_name", "Unknown") in ("", "Unknown", "Unknown Artist"):
            cand["agent_name"] = artist
        if not verify or await verify_item(client, cand):
            return cand
    return None


# ---------------------------------------------------------------- build
async def build_collection(db, spec, cache, *, limit=None, enrich=True, verify=True) -> dict:
    cid = spec["id"]
    logger.info(f"\n=== {spec['title']} ({cid}) ===")
    target = limit or spec.get("target", 20)

    out_items = []
    async with httpx.AsyncClient(headers=UA) as client:
        # 1) Curated "must-see" picks first — resolved AND verified (tries each source in turn).
        resolved, missing = [], []
        for pk in spec.get("picks", []):
            item = await resolve_pick(db, pk, spec, client, verify=verify)
            if item:
                resolved.append(item)
            else:
                missing.append(pk.get("title", "?"))
        if missing:
            logger.info(f"  MISSING (no verified PD source): {', '.join(str(m)[:32] for m in missing)}")

        # 2) Optional query-discovery supplement to top up toward the target. OPT-IN (default off):
        # keyword search cross-contaminates collections (name collisions like "Thomas"/"Turner"/
        # "Gustave", broad theme queries pull off-topic works) — CURATION-v2 audit found ~104 mis-filed
        # works from this. Curated picks are the source of truth; a collection must set
        # query_supplement=True explicitly to re-enable discovery.
        query_cands = []
        if spec.get("query_supplement", False) and spec.get("queries"):
            query_cands = await fetch_collection(db, spec)
        logger.info(f"  resolved {len(resolved)} picks (+{len(query_cands)} query candidates)")

        candidates = resolved + query_cands  # picks win ties in dedupe (listed first)
        selected = dedupe_and_select(candidates, target * 2 if verify else target)
        preverified = {r["source_url"] for r in resolved}

        for it in selected:
            if len(out_items) >= target:
                break
            su = it["source_url"]
            cached = cache.get(su)
            if cached and cached.get("verified"):
                out_items.append(cached["item"])
                continue
            if verify and su not in preverified:  # picks are already verified above
                ok = await verify_item(client, it)
                await asyncio.sleep(0.3)  # be polite to source CDNs
                if not ok:
                    logger.info(f"    ✗ dropped (not a display image): {it['title'][:48]}")
                    continue
            if enrich:
                it = enrich_item(it)
            else:
                _template_placard(it)
            cache[su] = {"item": it, "verified": True}
            out_items.append(it)
            logger.info(f"    ✓ {it['title'][:48]} — {it['agent_name'][:24]}")

    # Stamp the structured medium (CURATION-v2): a collection-level kind is the reliable signal the
    # free-text `medium` lacks — it gates the paintings-only Masterpieces first-glimpse downstream.
    kind = catalog_spec.kind_for(cid)
    for it in out_items:
        it["kind"] = kind
    return {
        "id": cid,
        "title": spec["title"],
        "description": spec["description"],
        "source": ", ".join(sorted({i["source"] for i in out_items})) or "Various",
        "license": spec.get("license", "Public Domain"),
        "items": out_items,
    }


async def main():
    ap = argparse.ArgumentParser(description="Build the Screen Docent catalog manifest.")
    ap.add_argument("--collection", help="build only this collection id")
    ap.add_argument("--limit", type=int, help="cap items per collection (for quick runs)")
    ap.add_argument("--no-enrich", action="store_true", help="skip model enrichment (template only)")
    ap.add_argument("--no-verify", action="store_true", help="skip image URL verification")
    args = ap.parse_args()

    enrich = not args.no_enrich
    verify = not args.no_verify
    if enrich and not ai_client.get_ai_config().get("configured"):
        logger.info("No AI model configured — using template placards (set one to enrich).")
        enrich = False

    specs = catalog_spec.COLLECTIONS
    if args.collection:
        specs = [s for s in specs if s["id"] == args.collection]
        if not specs:
            raise SystemExit(f"Unknown collection: {args.collection}")

    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    cache = load_cache()
    db = SessionLocal()

    # Preserve collections we're not rebuilding this run (so a single-collection run doesn't wipe the index).
    existing_index = {}
    idx_path = CATALOG_DIR / "index.json"
    if idx_path.exists():
        try:
            existing_index = {c["id"]: c for c in json.loads(idx_path.read_text()).get("collections", [])}
        except Exception:
            existing_index = {}

    summaries = dict(existing_index)
    try:
        for spec in specs:
            col = await build_collection(db, spec, cache, limit=args.limit, enrich=enrich, verify=verify)
            (CATALOG_DIR / f"{spec['id']}.json").write_text(json.dumps(col, indent=1, ensure_ascii=False))
            save_cache(cache)  # checkpoint after each collection
            if col["items"]:
                summaries[spec["id"]] = {
                    "id": col["id"], "title": col["title"], "description": col["description"],
                    "source": col["source"], "license": col["license"],
                    "count": len(col["items"]), "cover_thumbnail": col["items"][0]["thumbnail_url"],
                }
            else:
                summaries.pop(spec["id"], None)
            logger.info(f"  → wrote {len(col['items'])} items")
    finally:
        db.close()

    # Write the index in spec order, keeping only collections that have a file on disk.
    ordered = [summaries[s["id"]] for s in catalog_spec.COLLECTIONS
               if s["id"] in summaries and (CATALOG_DIR / f"{s['id']}.json").exists()]
    index = {"version": 1, "generated": datetime.now(UTC).isoformat(timespec="seconds"),
             "collections": ordered}
    idx_path.write_text(json.dumps(index, indent=1, ensure_ascii=False))
    total = sum(c["count"] for c in ordered)
    logger.info(f"\nDONE — {len(ordered)} collections, {total} items → {CATALOG_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
