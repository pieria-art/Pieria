"""Catalog browse + add-to-library — extracted from app.py (Phase 2 of the app-split refactor).

Split manifest produced by tools/build_catalog.py: an index.json (collection summaries) plus one
<collection_id>.json per collection (the items). Federated (subscribed) collections share the same
browse surface, namespaced with SUB_PREFIX so they can never collide with a bundled collection.
"""

import asyncio
import copy
import json
import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

import federation
from config import ARTWORK_ROOT, SUB_PREFIX
from core.downloads import _download_image_to_library, _focal_xy
from core.media import warm_canvas_cache_async
from core.settings_util import _catalog_remote_base, _fetch_remote_json
from database import get_db
from models import ArtworkModel, PlaylistModel, SubscriptionModel, playlist_artwork

logger = logging.getLogger("artwork-display-api")

router = APIRouter()

# SD_USER_AGENT (the descriptive UA Wikimedia/museums require) lives in config.py so the
# offline tools/ scripts can reuse it without importing this app.
CATALOG_DIR = Path("static/catalog")

# A2: mtime-keyed memo for the bundled catalog JSON. suggest_catalog/search_catalog walk every
# collection per keystroke and previously re-opened + re-parsed index.json + each <id>.json every time.
# We cache the parsed result keyed by (path, mtime) and hand back a deepcopy, because callers mutate
# what they get (_catalog_index does `.extend(subscribed)` / `setdefault("origin", ...)`) — a shared
# object would accumulate those mutations across calls.
_local_json_cache: dict = {}


def _read_local_json(path: Path):
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    hit = _local_json_cache.get(path)
    if hit and hit[0] == mtime:
        return copy.deepcopy(hit[1])
    with open(path) as f:
        data = json.load(f)
    _local_json_cache[path] = (mtime, data)
    return copy.deepcopy(data)


def _subscribed_summaries(db: Session) -> list:
    """Index summaries for enabled subscriptions, each stamped with its origin/trust/publisher."""
    out = []
    for sub in db.query(SubscriptionModel).filter(SubscriptionModel.enabled.is_(True)).all():
        if not sub.cached_manifest:
            continue
        try:
            m = json.loads(sub.cached_manifest)
        except ValueError:
            continue
        items = m.get("items", [])
        # The publisher can set an explicit collection cover; else fall back to the first item's image.
        cover = m.get("cover_image") or ""
        if not cover and items:
            img = items[0].get("image") or {}
            cover = img.get("thumbnail_url") or img.get("full_url") or ""
        out.append({
            "id": f"{SUB_PREFIX}{sub.id}",
            "title": m.get("title") or sub.title or "Untitled",
            "description": m.get("description", ""),
            "source": sub.publisher_name or "",
            "license": "",  # per-item; mixed
            "count": len(items),
            "cover_thumbnail": cover,
            "origin": "subscription",
            "trust": sub.trust,
            "publisher": {"id": sub.publisher_id, "name": sub.publisher_name, "url": sub.publisher_url},
        })
    return out


def _subscribed_collection(db: Session, collection_id: str):
    """Resolve a `sub_<id>` collection to its cached manifest's items (mapped to the catalog shape)."""
    try:
        sub_id = int(collection_id[len(SUB_PREFIX):])
    except ValueError:
        return None
    sub = db.query(SubscriptionModel).filter(
        SubscriptionModel.id == sub_id, SubscriptionModel.enabled.is_(True)).first()
    if not sub or not sub.cached_manifest:
        return None
    m = json.loads(sub.cached_manifest)
    return {
        "id": collection_id,
        "title": m.get("title"),
        "description": m.get("description", ""),
        "source": sub.publisher_name or "",
        "license": "",
        "origin": "subscription",
        "trust": sub.trust,
        "items": [federation.manifest_item_to_catalog(it) for it in m.get("items", [])],
    }


async def _catalog_index(db: Session) -> dict:
    """Collection summaries: optional remote override → bundled split files, then federated
    subscriptions appended. Bundled/remote collections are stamped origin='bundled' so the UI can
    distinguish official from subscribed."""
    index = None
    base = await _catalog_remote_base(db)
    if base:
        try:
            index = await _fetch_remote_json(base, "index.json")
        except Exception as e:
            logger.warning(f"[Catalog] remote index fetch failed ({e}); using bundled.")
    if index is None:
        index = _read_local_json(CATALOG_DIR / "index.json") or {"version": 1, "collections": []}
    for c in index.get("collections", []):
        c.setdefault("origin", "bundled")
    index.setdefault("collections", []).extend(_subscribed_summaries(db))
    return index

async def _catalog_collection(db: Session, collection_id: str):
    """One collection's full items file, or None if the id isn't present in the index."""
    if collection_id.startswith(SUB_PREFIX):
        return _subscribed_collection(db, collection_id)
    index = await _catalog_index(db)
    if not any(c.get("id") == collection_id for c in index.get("collections", [])):
        return None
    col = None
    base = await _catalog_remote_base(db)
    if base:
        try:
            col = await _fetch_remote_json(base, f"{collection_id}.json")
        except Exception as e:
            logger.warning(f"[Catalog] remote collection fetch failed ({e}); using bundled.")
    if col is None:
        col = _read_local_json(CATALOG_DIR / f"{collection_id}.json")
    if isinstance(col, dict):
        col.setdefault("origin", "bundled")
    return col


async def _download_and_create_artwork(db: Session, *, source_url: str, thumbnail_url: str,
                                       metadata: dict, playlist_id: Optional[int] = None,
                                       filename_prefix: str = "catalog") -> ArtworkModel:
    """Download a remote image once and create an *approved* ArtworkModel with prefilled metadata.
    Uses the shared robust downloader (UA + 429 backoff + validation), then optionally links the
    artwork into a playlist. Dedups on source_url — returns the existing row if already added."""
    existing = db.query(ArtworkModel).filter(ArtworkModel.source_url == source_url).first()
    if existing:
        return existing

    title = (metadata.get("title") or "art")
    filename = f"{filename_prefix}_{title.replace(' ', '_').lower()[:18]}"
    dest_path, safe_name, w, h = await _download_image_to_library(source_url, filename=filename)

    fx, fy = _focal_xy(metadata)   # baked catalog/manifest focal_point [x, y] (normalized); else centered

    artwork = ArtworkModel(
        filename=safe_name, original_width=w, original_height=h,
        crop_width=float(w), crop_height=float(h),
        focal_x=fx, focal_y=fy,
        status='approved',
        title=metadata.get("title"), agent_name=metadata.get("agent_name"),
        agent_role=metadata.get("agent_role", "Artist"), creation_date=metadata.get("creation_date"),
        cultural_context=metadata.get("cultural_context"), medium=metadata.get("medium"),
        date_display=metadata.get("date_display"), description_narrative=metadata.get("description_narrative"),
        tags=metadata.get("tags"), source_url=source_url, thumbnail_url=thumbnail_url, is_seed=False,
    )
    db.add(artwork); db.commit(); db.refresh(artwork)
    warm_canvas_cache_async(artwork.id, safe_name)   # pre-render the display image so it's warm by display time

    if playlist_id:
        playlist = db.query(PlaylistModel).filter(PlaylistModel.id == playlist_id).first()
        if playlist:
            try:
                (ARTWORK_ROOT / playlist.name).mkdir(parents=True, exist_ok=True)
                pl_path = ARTWORK_ROOT / playlist.name / safe_name
                if pl_path.is_symlink() or pl_path.exists():
                    pl_path.unlink()
                try: os.symlink(dest_path.resolve(), pl_path)
                except OSError: shutil.copy(dest_path, pl_path)
            except Exception as e:
                logger.warning(f"[Catalog] playlist symlink failed: {e}")
            order = len(db.execute(select(playlist_artwork.c.artwork_id).where(
                playlist_artwork.c.playlist_id == playlist.id)).all())
            try:
                db.execute(playlist_artwork.insert().values(
                    playlist_id=playlist.id, artwork_id=artwork.id, display_order=order))
                db.commit()
            except Exception:
                db.rollback()
    return artwork


@router.get("/api/catalog")
async def get_catalog(db: Session = Depends(get_db)):
    """Collection summaries (cover + count) for the Browse Catalog grid. Items load per-collection."""
    return await _catalog_index(db)

@router.get("/api/catalog/search")
async def search_catalog(q: str = "", db: Session = Depends(get_db)):
    """Flat keyword search across every bundled + subscribed catalog collection. Each hit is tagged
    with its `collection_id` + `item_index` so the existing add-path (`POST /api/catalog/add`) works
    unchanged. All whitespace-separated query tokens must match (AND) across title / artist / date /
    collection title. Defined *before* the `/{collection_id}` route so "search" isn't swallowed as a
    collection id. Capped to keep the payload small."""
    tokens = [t for t in q.lower().split() if t]
    if not tokens:
        return {"query": q, "results": []}
    added = {row[0] for row in db.query(ArtworkModel.source_url).filter(ArtworkModel.source_url.isnot(None)).all()}
    index = await _catalog_index(db)
    results = []
    CAP = 200
    for c in index.get("collections", []):
        cid = c.get("id")
        col = await _catalog_collection(db, cid)
        if not col:
            continue
        ctitle = col.get("title", "")
        for idx, it in enumerate(col.get("items", [])):
            hay = " ".join(str(it.get(k, "") or "") for k in ("title", "agent_name", "date_display"))
            hay = (hay + " " + ctitle).lower()
            if all(t in hay for t in tokens):
                results.append({**it, "collection_id": cid, "collection_title": ctitle,
                                "item_index": idx, "added": it.get("source_url") in added})
                if len(results) >= CAP:
                    break
        if len(results) >= CAP:
            break
    return {"query": q, "count": len(results), "results": results}

@router.get("/api/catalog/suggest")
async def suggest_catalog(q: str = "", db: Session = Depends(get_db)):
    """Lightweight autocomplete for the Museum search box — distinct artist names + titles from the
    catalog whose text contains the typed query, startswith matches ranked first. Backed by the
    mtime-keyed `_read_local_json` cache (A2), so repeat keystrokes don't re-read the catalog from disk.
    Defined *before* the `/{collection_id}` route so "suggest" isn't swallowed as a collection id."""
    ql = q.strip().lower()
    if len(ql) < 2:
        return {"query": q, "suggestions": []}
    index = await _catalog_index(db)
    seen, starts, contains = set(), [], []
    for c in index.get("collections", []):
        col = await _catalog_collection(db, c.get("id"))
        if not col:
            continue
        for it in col.get("items", []):
            for key in ("agent_name", "title"):   # artist first — the higher-signal suggestion
                term = (it.get(key) or "").strip()
                tl = term.lower()
                if not term or ql not in tl or tl in seen:
                    continue
                seen.add(tl)
                (starts if tl.startswith(ql) else contains).append(term)
    return {"query": q, "suggestions": (starts + contains)[:10]}

@router.get("/api/catalog/{collection_id}")
async def get_catalog_collection(collection_id: str, db: Session = Depends(get_db)):
    """One collection's items — prefilled placard metadata + hotlinked thumbnail_url + an `added`
    flag (matched by source_url). High-res is fetched only on add."""
    col = await _catalog_collection(db, collection_id)
    if not col:
        raise HTTPException(404, detail=f"Unknown collection: {collection_id}")
    added = {row[0] for row in db.query(ArtworkModel.source_url).filter(ArtworkModel.source_url.isnot(None)).all()}
    for it in col.get("items", []):
        it["added"] = it.get("source_url") in added
    return col

class CatalogAddPayload(BaseModel):
    collection_id: str
    item_index: int
    playlist_id: Optional[int] = None

@router.post("/api/catalog/add")
async def add_catalog_item(payload: CatalogAddPayload, db: Session = Depends(get_db)):
    """Lazily download one catalog item's high-res image and add it to the library (approved,
    metadata prefilled — no AI needed). Optionally links it to a playlist. Idempotent per source_url."""
    col = await _catalog_collection(db, payload.collection_id)
    if not col:
        raise HTTPException(404, detail=f"Unknown collection: {payload.collection_id}")
    items = col.get("items", [])
    if payload.item_index < 0 or payload.item_index >= len(items):
        raise HTTPException(404, detail="Unknown catalog item")
    item = items[payload.item_index]
    # Federated items come from a third party — SSRF-guard the image URL before the server fetches it
    # (a malicious manifest could point image.full_url at an internal/loopback address).
    if payload.collection_id.startswith(SUB_PREFIX):
        try:
            await asyncio.to_thread(federation._assert_public_url, item["source_url"])
        except federation.FederationError as e:
            raise HTTPException(400, detail=f"Refused to fetch image: {e}") from e
    art = await _download_and_create_artwork(
        db, source_url=item["source_url"], thumbnail_url=item.get("thumbnail_url"),
        metadata=item, playlist_id=payload.playlist_id)
    return {"status": "added", "artwork_id": art.id, "title": art.title}

class CatalogAddBulkPayload(BaseModel):
    items: List[CatalogAddPayload]      # each carries collection_id + item_index; per-item playlist ignored
    playlist_id: Optional[int] = None

@router.post("/api/catalog/add-bulk")
async def add_catalog_items_bulk(payload: CatalogAddBulkPayload, db: Session = Depends(get_db)):
    """Bulk version of /api/catalog/add — multi-select Add from the curated grid. Items may span
    collections (flat search results), so each carries its own collection_id + item_index. Best-effort:
    continues past individual failures; idempotent per source_url like the single add. Collections are
    resolved once and cached so a big batch from one collection doesn't re-load the manifest per item."""
    cache: dict = {}
    added, failed = 0, 0
    for it in payload.items:
        if it.collection_id not in cache:
            cache[it.collection_id] = await _catalog_collection(db, it.collection_id)
        col = cache[it.collection_id]
        items = col.get("items", []) if col else []
        if it.item_index < 0 or it.item_index >= len(items):
            failed += 1; continue
        item = items[it.item_index]
        # Federated items are third-party — SSRF-guard the image URL before the server fetches it.
        if it.collection_id.startswith(SUB_PREFIX):
            try:
                await asyncio.to_thread(federation._assert_public_url, item["source_url"])
            except federation.FederationError:
                failed += 1; continue
        try:
            await _download_and_create_artwork(
                db, source_url=item["source_url"], thumbnail_url=item.get("thumbnail_url"),
                metadata=item, playlist_id=payload.playlist_id)
            added += 1
        except Exception as e:
            failed += 1
            logger.warning(f"[Catalog] add-bulk item failed: {e}")
    return {"status": "done", "added": added, "failed": failed}

class CatalogAddCollectionPayload(BaseModel):
    collection_id: str
    playlist_id: Optional[int] = None

@router.post("/api/catalog/add-collection")
async def add_catalog_collection(payload: CatalogAddCollectionPayload, db: Session = Depends(get_db)):
    """Best-effort add of every item in a collection (continues past individual failures)."""
    col = await _catalog_collection(db, payload.collection_id)
    if not col:
        raise HTTPException(404, detail=f"Unknown collection: {payload.collection_id}")
    added, failed = 0, 0
    for item in col.get("items", []):
        try:
            await _download_and_create_artwork(
                db, source_url=item["source_url"], thumbnail_url=item.get("thumbnail_url"),
                metadata=item, playlist_id=payload.playlist_id)
            added += 1
        except Exception as e:
            failed += 1
            logger.warning(f"[Catalog] add-collection item failed: {e}")
    return {"status": "done", "added": added, "failed": failed}
