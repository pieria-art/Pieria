"""Catalog browse + add-to-library — extracted from app.py (Phase 2 of the app-split refactor).

Split manifest produced by tools/build_catalog.py: an index.json (collection summaries) plus one
<collection_id>.json per collection (the items). Federated (subscribed) collections share the same
browse surface, namespaced with SUB_PREFIX so they can never collide with a bundled collection.
"""

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

import core.licensing as core_licensing
import federation
from config import ARTWORK_ROOT, LIBRARY_DIR, SUB_PREFIX
from core.downloads import _aspect_crops, _download_image_to_library, _focal_xy
from core.media import warm_canvas_cache_async
from core.settings_util import _fetch_remote_json
from database import SessionLocal
from models import ArtworkModel, PlaylistModel, SettingsModel, SubscriptionModel, playlist_artwork

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


# ADR-148: catalog thumbnails live on R2; for an INSTALLED pack the same ~600px thumb is already on disk
# (ARTWORK_ROOT/_catalog_thumbs/<sha1(source_url)[:12]>.jpg), so those items are served locally and never
# touch the network. The id is a 12-hex pack hash — nothing else is ever joined into a path.
_THUMB_HASH_RE = re.compile(r"^[0-9a-f]{12}$")
_thumb_dir_cache: dict = {"key": None, "names": frozenset()}


def _installed_thumb_hashes() -> frozenset:
    d = Path(ARTWORK_ROOT) / "_catalog_thumbs"
    try:
        key = (str(d), d.stat().st_mtime_ns)
    except OSError:
        return frozenset()
    if _thumb_dir_cache["key"] != key:
        names = frozenset(p.stem for p in d.glob("*.jpg") if _THUMB_HASH_RE.match(p.stem))
        _thumb_dir_cache.update(key=key, names=names)
    return _thumb_dir_cache["names"]


def _localize_thumbs(items: list) -> None:
    """Point an item's thumbnail_url at the local thumb route when its installed pack thumb exists."""
    have = _installed_thumb_hashes()
    if not have:
        return
    for it in items:
        su = it.get("source_url")
        if not su:
            continue
        h = hashlib.sha1(su.encode("utf-8")).hexdigest()[:12]
        if h in have:
            it.setdefault("thumbnail_source_url", it.get("thumbnail_url"))
            it["thumbnail_url"] = f"/api/catalog/thumb/{h}"


def _norm(s) -> str:
    """Normalize a title/artist for owned-work matching across the bundled catalog and pack sentinels."""
    return (s or "").strip().lower()


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
    """Index summaries for enabled EXTERNAL subscriptions, each stamped with its origin/trust/publisher.
    `pack:` rows are installed local packs (ADR-044 "packs are subscriptions"), surfaced via /api/packs +
    the owned library — NOT here: their `pack:_catalog_thumbs/…` thumbnails don't resolve in a browser and
    would duplicate the bundled catalog's copy of the same works."""
    out = []
    for sub in db.query(SubscriptionModel).filter(
            SubscriptionModel.enabled.is_(True),
            SubscriptionModel.url.notlike("pack:%")).all():
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
        "items": [federation.manifest_item_to_catalog(it, default_license=m.get("default_license"))
                  for it in m.get("items", [])],
    }


# ADR-148: the async catalog helpers below never hold a request-scoped session. Each DB read is a small
# sync function run via run_in_threadpool on its OWN short-lived SessionLocal, so nothing sync touches the
# event loop and no session is shared across threads or held across a network await.

def _remote_base_sync() -> Optional[str]:
    """Optional remote override: a static base URL hosting index.json + <id>.json (the `catalog_url`
    setting — same read as core.settings_util._catalog_remote_base, on a short session)."""
    with SessionLocal() as db:
        setting = db.query(SettingsModel).filter(SettingsModel.setting_key == "catalog_url").first()
        return setting.setting_value.rstrip("/") if setting and setting.setting_value else None


def _subscribed_summaries_sync() -> list:
    with SessionLocal() as db:
        return _subscribed_summaries(db)


def _subscribed_collection_sync(collection_id: str):
    with SessionLocal() as db:
        return _subscribed_collection(db, collection_id)


async def _catalog_index() -> dict:
    """Collection summaries: optional remote override → bundled split files, then federated
    subscriptions appended. Bundled/remote collections are stamped origin='bundled' so the UI can
    distinguish official from subscribed."""
    index = None
    base = await run_in_threadpool(_remote_base_sync)
    if base:
        try:
            index = await _fetch_remote_json(base, "index.json")
        except Exception as e:
            logger.warning(f"[Catalog] remote index fetch failed ({e}); using bundled.")
    if index is None:
        index = _read_local_json(CATALOG_DIR / "index.json") or {"version": 1, "collections": []}
    for c in index.get("collections", []):
        c.setdefault("origin", "bundled")
    index.setdefault("collections", []).extend(await run_in_threadpool(_subscribed_summaries_sync))
    return index

async def _catalog_collection(collection_id: str, index: Optional[dict] = None):
    """One collection's full items file, or None if the id isn't present in the index. Callers that
    already hold the index (search/suggest walk every collection) pass it to skip rebuilding it."""
    if collection_id.startswith(SUB_PREFIX):
        return await run_in_threadpool(_subscribed_collection_sync, collection_id)
    if index is None:
        index = await _catalog_index()
    if not any(c.get("id") == collection_id for c in index.get("collections", [])):
        return None
    col = None
    base = await run_in_threadpool(_remote_base_sync)
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


def _find_existing_artwork(source_url: str) -> Optional[tuple]:
    with SessionLocal() as db:
        existing = db.query(ArtworkModel).filter(ArtworkModel.source_url == source_url).first()
        return (existing.id, existing.title) if existing else None


def _image_size(path: Path) -> tuple:
    with Image.open(path) as im:
        return im.size


def _create_artwork_row(*, dest_path: Path, safe_name: str, w: int, h: int, source_url: str,
                        thumbnail_url: str, metadata: dict, playlist_id: Optional[int]) -> tuple:
    """Insert the approved ArtworkModel and (optionally) link it into a playlist, on ONE short session
    (sync — callers run it via run_in_threadpool). Returns (artwork_id, title)."""
    fx, fy = _focal_xy(metadata)   # baked catalog/manifest focal_point [x, y] (normalized); else centered
    crops = _aspect_crops(metadata)   # baked catalog/manifest aspect_crops; else None (focal cover fallback)

    with SessionLocal() as db:
        artwork = ArtworkModel(
            filename=safe_name, original_width=w, original_height=h,
            crop_width=float(w), crop_height=float(h),
            focal_x=fx, focal_y=fy,
            aspect_crops_json=json.dumps(crops) if crops else None,
            status='approved',
            title=metadata.get("title"), agent_name=metadata.get("agent_name"),
            agent_role=metadata.get("agent_role", "Artist"), creation_date=metadata.get("creation_date"),
            cultural_context=metadata.get("cultural_context"), medium=metadata.get("medium"),
            date_display=metadata.get("date_display"), description_narrative=metadata.get("description_narrative"),
            tags=metadata.get("tags"), source_url=source_url, thumbnail_url=thumbnail_url, is_seed=False,
            license=core_licensing.normalize_license(metadata.get("license")),
            license_url=metadata.get("license_url") or None,
            attribution=metadata.get("attribution") or metadata.get("credit_line") or None,
            attribution_url=metadata.get("attribution_url") or None,
            origin_url=metadata.get("origin_url") or (source_url if source_url.startswith("http") else None),
        )
        db.add(artwork); db.commit(); db.refresh(artwork)
        art_id, art_title = artwork.id, artwork.title

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
                        playlist_id=playlist.id, artwork_id=art_id, display_order=order))
                    db.commit()
                except Exception:
                    db.rollback()
    return art_id, art_title


async def _download_and_create_artwork(*, source_url: str, thumbnail_url: str,
                                       metadata: dict, playlist_id: Optional[int] = None,
                                       filename_prefix: str = "catalog",
                                       local_file: Optional[str] = None) -> tuple:
    """Create an *approved* ArtworkModel with prefilled metadata, then optionally link it into a
    playlist. Two asset modes: a remote `source_url` is downloaded once (UA + 429 backoff + validation);
    a first-party pack item (`local_file` present + already under _Library/) is referenced in place with
    NO network (ADR-044). Dedups on source_url — returns the existing row if already added.
    Returns (artwork_id, title). Every DB step is its own threadpool call on a short session (ADR-148);
    no session is held across the download await."""
    existing = await run_in_threadpool(_find_existing_artwork, source_url)
    if existing:
        return existing

    if local_file and (LIBRARY_DIR / local_file).exists():
        # Local pack asset: the master is on disk — reference it, don't fetch.
        dest_path, safe_name = LIBRARY_DIR / local_file, local_file
        w, h = await run_in_threadpool(_image_size, dest_path)
    else:
        title = (metadata.get("title") or "art")
        filename = f"{filename_prefix}_{title.replace(' ', '_').lower()[:18]}"
        dest_path, safe_name, w, h = await _download_image_to_library(source_url, filename=filename)

    art_id, art_title = await run_in_threadpool(
        lambda: _create_artwork_row(dest_path=dest_path, safe_name=safe_name, w=w, h=h, source_url=source_url,
                                    thumbnail_url=thumbnail_url, metadata=metadata, playlist_id=playlist_id))
    warm_canvas_cache_async(art_id, safe_name)   # pre-render the display image so it's warm by display time
    return art_id, art_title


@router.get("/api/catalog")
async def get_catalog():
    """Collection summaries (cover + count) for the Browse Catalog grid. Items load per-collection."""
    return await _catalog_index()

@router.get("/api/catalog/search")
async def search_catalog(q: str = ""):
    """Flat keyword search across every bundled + subscribed catalog collection. Each hit is tagged
    with its `collection_id` + `item_index` so the existing add-path (`POST /api/catalog/add`) works
    unchanged. All whitespace-separated query tokens must match (AND) across title / artist / date /
    collection title. Defined *before* the `/{collection_id}` route so "search" isn't swallowed as a
    collection id. Capped to keep the payload small."""
    tokens = [t for t in q.lower().split() if t]
    if not tokens:
        return {"query": q, "results": []}
    # A work already in the library is "added" — matched by source_url, OR by (title, artist) so a bundled
    # catalog work owned via a pack (whose ArtworkModel.source_url is a `pack:<file>` sentinel, not the
    # museum URL) still shows the Added tag instead of a duplicate "Add to Library" button.
    def _owned():
        with SessionLocal() as db:
            lib = db.query(ArtworkModel.source_url, ArtworkModel.title, ArtworkModel.agent_name).all()
        return ({r[0] for r in lib if r[0]},
                {(_norm(r[1]), _norm(r[2])) for r in lib if r[1]})
    added_urls, added_keys = await run_in_threadpool(_owned)
    index = await _catalog_index()
    results = []
    seen = set()  # dedup the same work across collections (e.g. Mona Lisa in Masterpieces AND Renaissance)
    CAP = 200
    for c in index.get("collections", []):
        cid = c.get("id")
        col = await _catalog_collection(cid, index)
        if not col:
            continue
        ctitle = col.get("title", "")
        for idx, it in enumerate(col.get("items", [])):
            hay = " ".join(str(it.get(k, "") or "") for k in ("title", "agent_name", "date_display"))
            hay = (hay + " " + ctitle).lower()
            if all(t in hay for t in tokens):
                key = (_norm(it.get("title")), _norm(it.get("agent_name")))
                if key in seen:
                    continue  # one card per unique work; first collection wins the attribution
                seen.add(key)
                owned = (it.get("source_url") in added_urls or key in added_keys)
                _localize_thumbs([it])
                results.append({**it, "collection_id": cid, "collection_title": ctitle,
                                "item_index": idx, "added": owned})
                if len(results) >= CAP:
                    break
        if len(results) >= CAP:
            break
    return {"query": q, "count": len(results), "results": results}

@router.get("/api/catalog/suggest")
async def suggest_catalog(q: str = ""):
    """Lightweight autocomplete for the Museum search box — distinct artist names + titles from the
    catalog whose text contains the typed query, startswith matches ranked first. Backed by the
    mtime-keyed `_read_local_json` cache (A2), so repeat keystrokes don't re-read the catalog from disk.
    Defined *before* the `/{collection_id}` route so "suggest" isn't swallowed as a collection id."""
    ql = q.strip().lower()
    if len(ql) < 2:
        return {"query": q, "suggestions": []}
    index = await _catalog_index()
    seen, starts, contains = set(), [], []
    for c in index.get("collections", []):
        col = await _catalog_collection(c.get("id"), index)
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
async def get_catalog_collection(collection_id: str):
    """One collection's items — prefilled placard metadata + hotlinked thumbnail_url + an `added`
    flag (matched by source_url). High-res is fetched only on add.

    Items are ranked by `featured_rank` (fame, 0-100) descending — a stable sort, so ties keep their
    original order — meaning crowd-pleasers surface first instead of ~1000 works reading as flat
    decision-paralysis. Because that reorders the array, each item is stamped with `item_index`: the
    position in the *original* (unsorted) items list, which is what /api/catalog/add and add-bulk
    expect. Callers (the browse UI) must send back `item_index`, not the item's position in this
    response."""
    col = await _catalog_collection(collection_id)
    if not col:
        raise HTTPException(404, detail=f"Unknown collection: {collection_id}")
    def _added_urls():
        with SessionLocal() as db:
            return {row[0] for row in db.query(ArtworkModel.source_url).filter(
                ArtworkModel.source_url.isnot(None)).all()}
    added = await run_in_threadpool(_added_urls)
    ranked = sorted(enumerate(col.get("items", [])),
                    key=lambda pair: pair[1].get("featured_rank", 0), reverse=True)
    items = []
    for idx, it in ranked:
        it["added"] = it.get("source_url") in added
        it["item_index"] = idx
        items.append(it)
    _localize_thumbs(items)
    col["items"] = items
    return col


@router.get("/api/catalog/thumb/{thumb_hash}")
async def get_catalog_thumb(thumb_hash: str):
    """An installed pack's `_catalog_thumbs/<hash>.jpg`. Path-validated: only a 12-hex hash is accepted
    (no `..`, separators, or absolute paths can match), and the file must exist under that directory."""
    if not _THUMB_HASH_RE.match(thumb_hash):
        raise HTTPException(404, detail="No such thumbnail")
    root = (Path(ARTWORK_ROOT) / "_catalog_thumbs").resolve()
    path = (root / f"{thumb_hash}.jpg").resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(404, detail="No such thumbnail")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

class CatalogAddPayload(BaseModel):
    collection_id: str
    item_index: int
    playlist_id: Optional[int] = None

@router.post("/api/catalog/add")
async def add_catalog_item(payload: CatalogAddPayload):
    """Lazily download one catalog item's high-res image and add it to the library (approved,
    metadata prefilled — no AI needed). Optionally links it to a playlist. Idempotent per source_url."""
    col = await _catalog_collection(payload.collection_id)
    if not col:
        raise HTTPException(404, detail=f"Unknown collection: {payload.collection_id}")
    items = col.get("items", [])
    if payload.item_index < 0 or payload.item_index >= len(items):
        raise HTTPException(404, detail="Unknown catalog item")
    item = items[payload.item_index]
    # Federated REMOTE items come from a third party — SSRF-guard the image URL before the server
    # fetches it (a malicious manifest could point image.full_url at an internal/loopback address). A
    # first-party pack item (local_file) ships its bytes on disk: no fetch, so no SSRF check (ADR-044).
    if payload.collection_id.startswith(SUB_PREFIX) and not item.get("local_file"):
        try:
            await asyncio.to_thread(federation._assert_public_url, item["source_url"])
        except federation.FederationError as e:
            raise HTTPException(400, detail=f"Refused to fetch image: {e}") from e
    art_id, art_title = await _download_and_create_artwork(
        source_url=item["source_url"], thumbnail_url=item.get("thumbnail_url"),
        metadata=item, playlist_id=payload.playlist_id, local_file=item.get("local_file"))
    return {"status": "added", "artwork_id": art_id, "title": art_title}

class CatalogAddBulkPayload(BaseModel):
    items: List[CatalogAddPayload]      # each carries collection_id + item_index; per-item playlist ignored
    playlist_id: Optional[int] = None

@router.post("/api/catalog/add-bulk")
async def add_catalog_items_bulk(payload: CatalogAddBulkPayload):
    """Bulk version of /api/catalog/add — multi-select Add from the curated grid. Items may span
    collections (flat search results), so each carries its own collection_id + item_index. Best-effort:
    continues past individual failures; idempotent per source_url like the single add. Collections are
    resolved once and cached so a big batch from one collection doesn't re-load the manifest per item."""
    cache: dict = {}
    added, failed = 0, 0
    for it in payload.items:
        if it.collection_id not in cache:
            cache[it.collection_id] = await _catalog_collection(it.collection_id)
        col = cache[it.collection_id]
        items = col.get("items", []) if col else []
        if it.item_index < 0 or it.item_index >= len(items):
            failed += 1; continue
        item = items[it.item_index]
        # Remote federated items are third-party — SSRF-guard before fetching. Local pack items
        # (local_file) ship on disk — no fetch, no SSRF check (ADR-044).
        if it.collection_id.startswith(SUB_PREFIX) and not item.get("local_file"):
            try:
                await asyncio.to_thread(federation._assert_public_url, item["source_url"])
            except federation.FederationError:
                failed += 1; continue
        try:
            await _download_and_create_artwork(
                source_url=item["source_url"], thumbnail_url=item.get("thumbnail_url"),
                metadata=item, playlist_id=payload.playlist_id, local_file=item.get("local_file"))
            added += 1
        except Exception as e:
            failed += 1
            logger.warning(f"[Catalog] add-bulk item failed: {e}")
    return {"status": "done", "added": added, "failed": failed}

class CatalogAddCollectionPayload(BaseModel):
    collection_id: str
    playlist_id: Optional[int] = None


def _get_or_create_playlist_by_title(db: Session, name: str) -> PlaylistModel:
    """Get-or-create a (non-personal) playlist by name. Used by add-collection's no-playlist-chosen
    path so "add the whole collection" yields a playlist named after the collection — its identity —
    rather than a nameless library dump. `PlaylistModel.name` is unique, so a repeat add-collection
    call (or a playlist the user already made with that name) is reused, not duplicated."""
    pl = db.query(PlaylistModel).filter(PlaylistModel.name == name).first()
    if not pl:
        pl = PlaylistModel(name=name)
        db.add(pl); db.commit(); db.refresh(pl)
    return pl

@router.post("/api/catalog/add-collection")
async def add_catalog_collection(payload: CatalogAddCollectionPayload):
    """Best-effort add of every item in a collection (continues past individual failures).

    Back-compatible: an explicit playlist_id is honored unchanged. When none is given, get-or-create
    a playlist named after the collection's title (falling back to its id) so the collection's
    identity survives instead of a nameless dump into the plain library."""
    col = await _catalog_collection(payload.collection_id)
    if not col:
        raise HTTPException(404, detail=f"Unknown collection: {payload.collection_id}")
    playlist_id = payload.playlist_id
    if playlist_id is None:
        title = col.get("title") or payload.collection_id
        def _playlist_id_for(name):
            with SessionLocal() as db:
                return _get_or_create_playlist_by_title(db, name).id
        playlist_id = await run_in_threadpool(_playlist_id_for, title)
    added, failed = 0, 0
    for item in col.get("items", []):
        try:
            await _download_and_create_artwork(
                source_url=item["source_url"], thumbnail_url=item.get("thumbnail_url"),
                metadata=item, playlist_id=playlist_id)
            added += 1
        except Exception as e:
            failed += 1
            logger.warning(f"[Catalog] add-collection item failed: {e}")
    return {"status": "done", "added": added, "failed": failed}
