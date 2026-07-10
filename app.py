#!/usr/bin/env python3
"""
FastAPI Backend for the Artwork Display Engine.
Phase 4: Targeted WebSocket Routing for Multiple Displays.
"""

import asyncio
import copy
import fcntl
import html
import io
import json
import logging
import os
import re
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pillow_heif
from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

# Load environment variables
load_dotenv()

# Teach Pillow to read HEIC/HEIF so iPhone photos (the default capture format) decode through the
# normal Image.open() path everywhere. Upload handlers transcode to a browser-renderable format —
# browsers can't display HEIC either, so decode alone isn't enough.
pillow_heif.register_heif_opener()

# -----------------------------------------------------------------------------
# 1. Configuration, Logging & Targeted WebSocket Manager
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("artwork-display-api")

# Local imports
import httpx

import curator
import scout
from agents import process_artwork

# Targeted WebSocket connection registry (shared by the ws + remote push paths).
from core.connections import ConnectionManager, manager  # noqa: F401
from database import SessionLocal, get_db
from models import (
    ActiveDisplayModel,
    ArtworkModel,
    DiscoveryQueueModel,
    PlaylistModel,
    PublisherCollectionModel,
    RemoteCommandModel,
    SettingsModel,
    SubscriptionModel,
    playlist_artwork,
)
from query_classifier import QueryClassifier
from result_ranker import ResultRanker, clean_title
from scout import create_search_session, get_search_session

# Shared instances for smart search
_query_classifier = QueryClassifier()
_result_ranker = ResultRanker()

import ai_client
import config
import federation
import frame_push
import host_health
import publisher
from config import ARTWORK_ROOT, LIBRARY_DIR, strip_markdown

# SSRF-safe downloader + focal-point parsing (see core/downloads.py).
from core.downloads import _download_image_to_library, _focal_xy  # noqa: E402

# Derivative-image rendering primitives (see core/media.py).
from core.media import (  # noqa: E402
    DERIVATIVES_DIR,
    DISPLAY_MAX_EDGE,  # noqa: F401  — re-exported for tests/test_display_image.py
    get_optimized_image,
    render_canvas_image,
    warm_canvas_cache_async,
)
from core.playback import (  # noqa: E402
    _display_now_playing,
    _now_playing_artwork,
    _playlist_name_if_playable,
    select_next_image,
    touch_active_display,
)

# Origin/CORS trust checks used by the middleware below (see core/security.py).
from core.security import (  # noqa: E402
    _PUBLIC_FEED_GET_PREFIXES,
    _origin_allowed,
    _same_origin,  # noqa: F401  — re-exported; used only by the middleware below
)

# Settings-table read/write + schedule helpers (see core/settings_util.py).
from core.settings_util import (  # noqa: E402
    DEFAULT_SCHEDULE,  # noqa: F401  — re-exported for tests/test_schedule.py
    SCHEDULE_SETTING_KEY,
    _catalog_remote_base,
    _fetch_remote_json,
    _load_schedule,
    _upsert_setting,
)
from epaper import PALETTES, VALID_FORMATS, media_type_for, render_for_epaper


async def warm_all_canvas_cache() -> None:
    """Leader boot task: pre-render the capped display image for every approved artwork so the Canvas
    never stalls on first display (esp. huge museum originals on a Pi). Sequential — one encode at a
    time — to avoid a CPU storm while the server is also serving; `render_canvas_image` skips anything
    already cached, so reruns are cheap. Best-effort per item."""
    db = SessionLocal()
    try:
        arts = (db.query(ArtworkModel.id, ArtworkModel.filename)
                .filter(ArtworkModel.status == "approved").all())
    finally:
        db.close()
    logger.info(f"[Warm] pre-rendering display derivatives for {len(arts)} artworks...")
    done = 0
    for art_id, filename in arts:
        try:
            await run_in_threadpool(render_canvas_image, LIBRARY_DIR / filename, art_id)
            done += 1
        except Exception as e:
            logger.warning(f"[Warm] art {art_id} ({filename}): {e}")
    logger.info(f"[Warm] display cache warm complete ({done}/{len(arts)}).")

async def run_ai_pipeline(artwork_id: int):
    db = SessionLocal()
    try:
        await process_artwork(artwork_id, db)
    finally:
        db.close()

async def run_rag_pipeline(artwork_id: int, context_hints: str = None):
    db = SessionLocal()
    try:
        await curator.enrich_artwork(artwork_id, db, context_hints=context_hints)
    finally:
        db.close()

async def run_scouts_bg(query: str = None, sources: List[str] = None,
                       session_id: str = None, limit: int = 10):
    """Background task: classifies query, runs scouts, ranks results, inserts into DB."""
    db = SessionLocal()
    try:
        # Retrieve or create search session
        session = get_search_session(session_id) if session_id else None
        if session:
            intent = session.intent
            offset = session.offset
        else:
            # B1: classify() → sync ai_client.chat (httpx, 90s default) — thread it so a slow provider
            # can't stall this worker's loop.
            intent = await asyncio.to_thread(_query_classifier.classify, query) if query else None
            offset = 0

        logger.info(f"[Scout BG] Starting scouts: query='{query}', sources={sources}, "
                    f"intent={intent.query_type if intent else 'none'}, "
                    f"canonical='{intent.canonical_name if intent else 'n/a'}', "
                    f"offset={offset}, limit={limit}")

        # Run scouts with classified intent
        raw_results = await scout.run_scouts(
            db, query=query, sources=sources,
            intent=intent, offset=offset, limit=limit
        )
        logger.info(f"[Scout BG] Scouts returned {len(raw_results)} raw results")

        # Rank and deduplicate
        ranked_results = _result_ranker.rank_and_deduplicate(raw_results, intent)
        logger.info(f"[Scout BG] After ranking: {len(ranked_results)} results")

        # Insert into DiscoveryQueue, skipping duplicates
        total_new = 0
        for item in ranked_results:
            existing = db.query(DiscoveryQueueModel).filter(
                DiscoveryQueueModel.source_url == item['source_url']
            ).first()
            if not existing:
                item['proposed_title'] = clean_title(item.get('proposed_title'))
                new_entry = DiscoveryQueueModel(
                    **item,
                    search_session_id=session_id
                )
                db.add(new_entry)
                total_new += 1
        db.commit()
        logger.info(f"[Scout BG] DiscoveryQueue updated with {total_new} new items.")
    except Exception as e:
        logger.error(f"[Scout BG] BACKGROUND TASK FAILED: {e}", exc_info=True)
    finally:
        db.close()

async def run_batch_enrich_bg():
    db = SessionLocal()
    try:
        await curator.batch_enrich_all(db)
    finally:
        db.close()

def sync_db_with_filesystem(db: Session) -> None:
    if not ARTWORK_ROOT.exists():
        ARTWORK_ROOT.mkdir(parents=True, exist_ok=True)
    if not LIBRARY_DIR.exists():
        LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

    valid_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    for item in ARTWORK_ROOT.iterdir():
        # Skip internal dirs (underscore-prefixed: _Library canonical store, _derivatives display cache).
        # They are NOT collections — enumerating them here would mint a bogus playlist and absorb cache files.
        if item.is_dir() and not item.name.startswith("_"):
            playlist = db.query(PlaylistModel).filter(PlaylistModel.name == item.name).first()
            if not playlist:
                playlist = PlaylistModel(name=item.name)
                db.add(playlist); db.commit(); db.refresh(playlist)

            for file_path in item.iterdir():
                if file_path.suffix.lower() in valid_extensions:
                    dest_path = LIBRARY_DIR / file_path.name
                    if not dest_path.exists():
                        shutil.move(file_path, dest_path)

                    artwork = db.query(ArtworkModel).filter(ArtworkModel.filename == file_path.name).first()
                    if not artwork:
                        with Image.open(dest_path) as img:
                            w, h = img.size
                        artwork = ArtworkModel(
                            filename=file_path.name,
                            original_width=w, original_height=h,
                            status='approved'
                        )
                        db.add(artwork); db.commit(); db.refresh(artwork)

                    existing_link = db.execute(
                        select(playlist_artwork).where(
                            playlist_artwork.c.playlist_id == playlist.id,
                            playlist_artwork.c.artwork_id == artwork.id
                        )
                    ).first()

                    if not existing_link:
                        db.execute(playlist_artwork.insert().values(
                            playlist_id=playlist.id,
                            artwork_id=artwork.id,
                            display_order=0
                        ))
            db.commit()

async def run_factory_seed(db: Session):
    """Parses factory_seed.json and injects masterpieces if library is empty."""
    seed_file = Path("static/factory_seed.json")
    if not seed_file.exists(): return

    existing = db.query(ArtworkModel).filter(ArtworkModel.is_seed == True).first()
    if existing: return

    try:
        import json
        with open(seed_file) as f:
            seeds = json.load(f)

        logger.info(f"[Bootstrapper] Injecting {len(seeds)} Masterpieces from Factory Seed...")

        async def perform_downloads(seed_items: list):
            db_local = SessionLocal()
            try:
                await asyncio.sleep(2)
                for idx, item in enumerate(seed_items):
                    await asyncio.sleep(2.0)
                    try:
                        pl_name = item.get("playlist", "The Masterpieces")
                        playlist = db_local.query(PlaylistModel).filter(PlaylistModel.name == pl_name).first()
                        if not playlist:
                            playlist = PlaylistModel(name=pl_name)
                            db_local.add(playlist); db_local.commit(); db_local.refresh(playlist)
                            (ARTWORK_ROOT / pl_name).mkdir(parents=True, exist_ok=True)

                        filename = f"seed_{idx}_{item.get('title', 'art').replace(' ','_').lower()[:15]}"
                        logger.info(f"[Bootstrapper] Downloading '{filename}'...")

                        # Shared robust downloader (UA + 429 retry + validation).
                        try:
                            dest_path, safe_name, w, h = await _download_image_to_library(
                                item.get("source_url"), filename=filename)
                        except HTTPException as e:
                            logger.error(f"[Bootstrapper] Failed download {filename}: {e.detail}")
                            continue

                        pl_path = ARTWORK_ROOT / pl_name / safe_name
                        # Remove stale symlink before creating new one
                        if pl_path.is_symlink() or pl_path.exists():
                            pl_path.unlink()
                        try: os.symlink(dest_path.resolve(), pl_path)
                        except OSError: shutil.copy(dest_path, pl_path)

                        sfx, sfy = _focal_xy(item)
                        artwork = ArtworkModel(
                            filename=safe_name, original_width=w, original_height=h,
                            crop_width=float(w), crop_height=float(h),
                            status='approved',
                            title=item.get("title"), agent_name=item.get("agent_name"),
                            agent_role=item.get("agent_role"), creation_date=item.get("creation_date"),
                            cultural_context=item.get("cultural_context"), medium=item.get("medium"),
                            date_display=item.get("date_display"), description_narrative=item.get("description_narrative"),
                            tags=item.get("tags"), is_seed=True,
                            focal_x=sfx, focal_y=sfy,
                        )
                        db_local.add(artwork); db_local.commit(); db_local.refresh(artwork)

                        try:
                            db_local.execute(playlist_artwork.insert().values(
                                playlist_id=playlist.id, artwork_id=artwork.id, display_order=idx
                            ))
                            db_local.commit()
                        except Exception:
                            db_local.rollback()  # playlist_artwork may already exist

                        logger.info(f"[Bootstrapper] ✓ Seeded '{item.get('title')}' → {pl_name}")

                    except Exception as inner_e: logger.error(f"[Bootstrapper] Item error: {inner_e}")
            finally: db_local.close()

        asyncio.create_task(perform_downloads(seeds))

    except Exception as e:
        logger.error(f"[Bootstrapper] Failed to parse factory_seed.json: {e}")

from db_migrate import run_migrations


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle events for FastAPI application with multi-worker concurrency locks."""

    # Leader Election using fcntl: the first worker grabs the exclusive non-blocking lock
    # and runs exclusive boot tasks; the other workers get BlockingIOError and skip them.
    # We deliberately never unlock — the OS releases the flock when the worker process exits,
    # so a slightly delayed follower can't grab it mid-boot and race the migrations.
    lock_file = open("/tmp/screen_docent_startup.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info("[Startup] Follower worker initialized. Skipping exclusive boot tasks.")
        yield
        return

    logger.info("[Startup] Leader elected. Running exclusive boot tasks (migrations, filesystem sync)...")

    # 1) Schema: Alembic is the single source of truth (create_all no longer runs at boot).
    #    A migration failure MUST halt startup — it is caught at deploy, not by a user's
    #    black screen. Deliberately NOT wrapped in a swallowing try/except (see ADR-035).
    logger.info("Running Alembic migrations...")
    run_migrations()
    logger.info("Alembic migrations complete.")

    # 2) Best-effort init: a hiccup in filesystem sync / seed / warmers should not wedge the
    #    whole box, so these stay tolerant (unlike migrations above).
    try:
        db = SessionLocal()
        try:
            sync_db_with_filesystem(db)
            await run_factory_seed(db)
        finally:
            db.close()

        # Pre-render the capped Canvas derivatives in the background so the display never stalls on
        # the one-time encode of a huge original (leader-only; runs while the server serves traffic).
        asyncio.create_task(warm_all_canvas_cache())

        # Leader-only: the Samsung Frame TV pusher. Running it solely in the leader avoids
        # firing it once per uvicorn worker. No-op until enabled in Settings → Frame TV.
        asyncio.create_task(frame_push.frame_push_loop(_frame_select))
        logger.info("[Startup] Frame TV push loop scheduled (leader).")
    except Exception as e:
        logger.error(f"[Startup] Non-fatal error during initialization: {e}", exc_info=True)

    yield

app = FastAPI(title="Artwork Display Engine API", version="0.4.5", lifespan=lifespan)

@app.middleware("http")
async def inject_aggressive_cache_headers(request: Request, call_next):
    response = await call_next(request)
    # Target Pillow rendering routes, media library, and static assets
    path = request.url.path
    is_media_cacheable = (
        (path.startswith("/artworks/") and ("thumbnail" in path or "preview" in path))
        or path.startswith("/media/")
        or path.endswith((".svg", ".png", ".jpg", ".webp"))
    )
    is_code_asset = path.endswith((".css", ".js", ".json"))
    is_html_asset = path.endswith(".html") or path in ("/admin", "/remote", "/studio", "/help", "/")

    if path.startswith("/api/") or is_html_asset or path.startswith("/display/"):
        # API/HTML and the per-display e-ink endpoint must never be cached.
        # (/display/*.png must beat the is_media_cacheable .png rule below.)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    elif is_media_cacheable:
        # Images/media rarely change — cache aggressively
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif is_code_asset:
        # JS/CSS/JSON change during development — short cache + revalidate
        response.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
    # Security headers (L2). nosniff is defense-in-depth against the /media MIME-confusion XSS class
    # (H1); Referrer-Policy keeps LAN paths out of any outbound Referer.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


# --- CORS + cross-origin state-change guard (ADR-036) ------------------------------------------------
# The app is a no-login LAN kiosk (ADR-013/015): the trust boundary is "you are a device on my LAN".
# The old wildcard `CORSMiddleware(allow_origins=["*"], allow_credentials=True)` silently widened that
# to "any browser tab on the LAN can drive the full API cross-origin". We replace it with a policy that
# keeps the no-login model honest:
#   * Cross-origin STATE CHANGES are blocked — a hostile page (bad ad, phishing tab) always sends an
#     Origin header on a cross-origin state-changing fetch; curl/native integrations send none and are
#     allowed (the accepted LAN-presence risk, unchanged).
#   * The read-only public FEED (what integrations consume) stays cross-origin readable.
#   * Admin/library GETs are NOT cross-origin readable (no ACAO) — a hostile tab can't exfiltrate them.
#   * Same-origin (the kiosk's own page) and explicitly configured SD_ALLOWED_ORIGINS always pass.
# _same_origin, _origin_allowed, _PUBLIC_FEED_GET_PREFIXES now live in core/security.py (imported above).
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@app.middleware("http")
async def cors_and_origin_guard(request: Request, call_next):
    origin = request.headers.get("origin", "")
    host = request.headers.get("host", "")
    path = request.url.path
    allowed = _origin_allowed(origin, host)
    # A CORS preflight advertises the real method it is clearing; judge against that, not OPTIONS.
    effective_method = (request.headers.get("access-control-request-method", "GET").upper()
                        if request.method == "OPTIONS" else request.method)
    is_public_read = effective_method in ("GET", "HEAD") and path.startswith(_PUBLIC_FEED_GET_PREFIXES)

    # The teeth: refuse a cross-origin state change from a browser tab (blocks the preflight too).
    if effective_method in _MUTATING_METHODS and origin and not allowed:
        return Response("cross-origin request blocked", status_code=403)

    if request.method == "OPTIONS" and origin:
        resp = Response(status_code=204)
    else:
        resp = await call_next(request)

    if origin and (is_public_read or allowed):
        resp.headers["Access-Control-Allow-Origin"] = "*" if is_public_read else origin
        resp.headers["Vary"] = "Origin"
        if request.method == "OPTIONS":
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = (
                request.headers.get("access-control-request-headers") or "*")
            resp.headers["Access-Control-Max-Age"] = "600"
    return resp



# -----------------------------------------------------------------------------
# 2. Data Models
# -----------------------------------------------------------------------------
class ArtworkSchema(BaseModel):
    id: int
    filename: str
    original_width: int
    original_height: int
    title: Optional[str] = None
    agent_name: Optional[str] = None
    agent_role: Optional[str] = None
    creation_date: Optional[str] = None; cultural_context: Optional[str] = None
    medium: Optional[str] = None; date_display: Optional[str] = None
    description_narrative: Optional[str] = None; tags: Optional[str] = None
    status: str
    crop_x: float
    crop_y: float
    crop_width: float
    crop_height: float
    focal_x: float = 0.5
    focal_y: float = 0.5
    is_personal: bool = False
    model_config = {"from_attributes": True}

class PlaylistSchema(BaseModel):
    id: int
    name: str
    display_time: int
    default_mode: str
    shuffle: bool
    placard_initial_wait_sec: int
    placard_initial_show_sec: int
    placard_interaction_show_sec: int
    artworks: List[ArtworkSchema] = []
    @property
    def image_count(self) -> int:
        return len(self.artworks)
    model_config = {"from_attributes": True}

class ArtworkApproval(BaseModel):
    title: str; agent_name: str; agent_role: str; creation_date: str; cultural_context: str; medium: str; date_display: str; description_narrative: str; tags: str

class PlaylistUpdate(BaseModel):
    name: Optional[str] = None
    display_time: Optional[int] = None
    default_mode: Optional[str] = None
    shuffle: Optional[bool] = None
    placard_initial_wait_sec: Optional[int] = None
    placard_initial_show_sec: Optional[int] = None
    placard_interaction_show_sec: Optional[int] = None

class ReorderRequest(BaseModel):
    artwork_ids: List[int]

class ArtworkIds(BaseModel):
    """A list of artwork ids, for the bulk multi-select actions (add/remove/delete)."""
    artwork_ids: List[int]

class RemoteChangeRequest(BaseModel):
    target_display: str
    action: str
    playlist: Optional[str] = None
    mode: Optional[str] = None

class RegenerationRequest(BaseModel):
    hint: Optional[str] = None

class DispatchRequest(BaseModel):
    sources: List[str]
    search: Optional[str] = None
    limit: int = 10

class LoadMoreRequest(BaseModel):
    session_id: str

class DiscoveryQueueSchema(BaseModel):
    id: int
    source_url: str
    thumbnail_url: str
    proposed_title: Optional[str] = None
    proposed_artist: Optional[str] = None
    source_api: str
    status: str
    relevance_score: Optional[float] = 0.0
    search_session_id: Optional[str] = None
    model_config = {"from_attributes": True}

# -----------------------------------------------------------------------------
# 3. API Endpoints
# -----------------------------------------------------------------------------

@app.get("/artworks", response_model=List[ArtworkSchema])
async def get_full_library(db: Session = Depends(get_db)):
    return db.query(ArtworkModel).all()

@app.get("/playlists", response_model=List[PlaylistSchema])
async def list_playlists(db: Session = Depends(get_db)):
    # Underscore-prefixed names are internal pseudo-collections (e.g. "_derivatives", the optimized-image
    # display cache) — never real collections. Keep their rows + cached images, but never surface them in
    # the UI. Mirrors the sync-time skip of "_"-prefixed dirs; this is the matching display-layer guard,
    # so even a stale "_" playlist (created before that skip existed) stays hidden everywhere /playlists
    # feeds: the admin sidebar, the "Add to" picker, and the Canvas first-non-empty fallback.
    return [p for p in db.query(PlaylistModel).all() if not p.name.startswith("_")]

@app.post("/playlists", response_model=PlaylistSchema)
async def create_playlist(name: str = Form(...), db: Session = Depends(get_db)):
    existing = db.query(PlaylistModel).filter(PlaylistModel.name == name).first()
    if existing: raise HTTPException(status_code=400, detail="Exists")
    new_p = PlaylistModel(name=name); db.add(new_p); db.commit(); db.refresh(new_p)
    return new_p

@app.patch("/playlists/{playlist_id}", response_model=PlaylistSchema)
async def update_playlist(playlist_id: int, data: PlaylistUpdate, db: Session = Depends(get_db)):
    p = db.query(PlaylistModel).filter(PlaylistModel.id == playlist_id).first()
    if not p: raise HTTPException(status_code=404)
    if data.name is not None:   # A4: rename (collision-guarded, no empty/internal "_" names)
        new_name = data.name.strip()
        if not new_name or new_name.startswith("_"):
            raise HTTPException(400, detail="Invalid collection name")
        if new_name != p.name:
            clash = db.query(PlaylistModel).filter(PlaylistModel.name == new_name,
                                                   PlaylistModel.id != playlist_id).first()
            if clash: raise HTTPException(400, detail="A collection with that name already exists")
            p.name = new_name
    if data.display_time is not None: p.display_time = data.display_time
    if data.default_mode is not None: p.default_mode = data.default_mode
    if data.shuffle is not None: p.shuffle = data.shuffle
    if data.placard_initial_wait_sec is not None: p.placard_initial_wait_sec = data.placard_initial_wait_sec
    if data.placard_initial_show_sec is not None: p.placard_initial_show_sec = data.placard_initial_show_sec
    if data.placard_interaction_show_sec is not None: p.placard_interaction_show_sec = data.placard_interaction_show_sec
    db.commit(); db.refresh(p); return p

@app.delete("/playlists/{playlist_id}")
async def delete_playlist(playlist_id: int, db: Session = Depends(get_db)):
    p = db.query(PlaylistModel).filter(PlaylistModel.id == playlist_id).first()
    if not p: raise HTTPException(404)
    db.delete(p); db.commit(); return {"status": "ok"}

@app.post("/playlists/{playlist_id}/artworks/{artwork_id}")
async def link_artwork_to_playlist(playlist_id: int, artwork_id: int, db: Session = Depends(get_db)):
    db.execute(playlist_artwork.insert().values(playlist_id=playlist_id, artwork_id=artwork_id))
    db.commit(); return {"status": "linked"}

@app.delete("/playlists/{playlist_id}/artworks/{artwork_id}")
async def unlink_artwork_from_playlist(playlist_id: int, artwork_id: int, db: Session = Depends(get_db)):
    db.execute(delete(playlist_artwork).where(
        playlist_artwork.c.playlist_id == playlist_id,
        playlist_artwork.c.artwork_id == artwork_id
    ))
    db.commit(); return {"status": "unlinked"}

@app.post("/playlists/{playlist_id}/artworks")
async def link_artworks_to_playlist(playlist_id: int, payload: ArtworkIds, db: Session = Depends(get_db)):
    """Bulk add: link many library artworks to a playlist in one call (the multi-select 'Add from
    Library'). Idempotent per artwork — reuses _link_artwork_to_playlist, which skips existing links
    and appends in order. A distinct path from the single /{artwork_id} POST, so no route collision."""
    for aid in payload.artwork_ids:
        _link_artwork_to_playlist(db, playlist_id, aid)
    return {"status": "linked", "count": len(payload.artwork_ids)}

@app.delete("/playlists/{playlist_id}/artworks")
async def unlink_artworks_from_playlist(playlist_id: int, payload: ArtworkIds, db: Session = Depends(get_db)):
    """Bulk remove: unlink many artworks from a playlist (multi-select Remove). Removes only the
    association — the artworks stay in the library."""
    n = db.execute(delete(playlist_artwork).where(
        playlist_artwork.c.playlist_id == playlist_id,
        playlist_artwork.c.artwork_id.in_(payload.artwork_ids or [-1]))).rowcount
    db.commit(); return {"status": "unlinked", "count": n}

@app.post("/playlists/{playlist_id}/reorder")
async def reorder_playlist(playlist_id: int, request: ReorderRequest, db: Session = Depends(get_db)):
    for index, art_id in enumerate(request.artwork_ids):
        db.execute(update(playlist_artwork).where(
            playlist_artwork.c.playlist_id == playlist_id,
            playlist_artwork.c.artwork_id == art_id
        ).values(display_order=index))
    db.commit(); return {"status": "success"}

@app.post("/upload", response_model=ArtworkSchema)
async def upload_artwork(background_tasks: BackgroundTasks, file: UploadFile = File(...), playlist_id: Optional[int] = Form(None), db: Session = Depends(get_db)):
    if not LIBRARY_DIR.exists(): LIBRARY_DIR.mkdir(parents=True)
    raw = await file.read()

    def _decode_and_store():
        # NEVER build the on-disk path from the client-supplied filename (C1: it was written verbatim,
        # giving any LAN client an unauth path-traversal / arbitrary-write primitive). Derive a safe base
        # from the filename *stem only* (Path().stem strips directories) and pick the extension from
        # Pillow's own detected format. HEIC/HEIF is transcoded to JPEG; every other format keeps its
        # exact bytes. All of this (decode/transpose/encode + disk write) is blocking → run in a thread.
        with Image.open(io.BytesIO(raw)) as src:
            fmt = (src.format or "").upper()
            ext = {"JPEG": ".jpg", "HEIF": ".jpg", "HEIC": ".jpg", "PNG": ".png", "WEBP": ".webp",
                   "GIF": ".gif", "BMP": ".bmp", "TIFF": ".tiff"}.get(fmt, f".{fmt.lower()}" if fmt else ".jpg")
            stem = Path(file.filename or "").stem
            base = "".join(c for c in stem if c.isalnum() or c in " _-").strip()
            base = (base.replace(" ", "_")[:24] or "artwork").lower()
            fname = f"upload_{base}{ext}"
            dest = LIBRARY_DIR / fname
            n = 1
            while dest.exists():
                fname = f"upload_{base}_{n}{ext}"; dest = LIBRARY_DIR / fname; n += 1

            if fmt in ("HEIF", "HEIC"):
                img = ImageOps.exif_transpose(src)
                if img.mode not in ("RGB", "L"): img = img.convert("RGB")
                img.save(dest, format="JPEG", quality=92)
                return fname, *img.size
            dest.write_bytes(raw)
            return fname, *src.size

    try:
        fname, w, h = await run_in_threadpool(_decode_and_store)
    except Exception:
        raise HTTPException(400, detail="That file isn't a readable image.")
    new_a = ArtworkModel(filename=fname, original_width=w, original_height=h, status='pending_review')
    db.add(new_a); db.commit(); db.refresh(new_a)
    if playlist_id:
        db.execute(playlist_artwork.insert().values(playlist_id=playlist_id, artwork_id=new_a.id))
        db.commit()
    background_tasks.add_task(run_ai_pipeline, new_a.id)
    return new_a


PERSONAL_PLAYLIST_NAME = "My Photos"


def _get_or_create_playlist(db: Session, name: str, is_personal: bool = False) -> PlaylistModel:
    pl = db.query(PlaylistModel).filter(PlaylistModel.name == name).first()
    if not pl:
        pl = PlaylistModel(name=name, is_personal=is_personal)
        db.add(pl); db.commit(); db.refresh(pl)
    elif is_personal and not pl.is_personal:
        pl.is_personal = True; db.commit()   # self-heal a pre-existing "My Photos" created before the flag
    return pl


def _link_artwork_to_playlist(db: Session, playlist_id: int, artwork_id: int) -> None:
    """Append an artwork to a playlist (idempotent), preserving append order."""
    exists = db.execute(select(playlist_artwork).where(
        playlist_artwork.c.playlist_id == playlist_id,
        playlist_artwork.c.artwork_id == artwork_id)).first()
    if exists:
        return
    order = len(db.execute(select(playlist_artwork.c.artwork_id).where(
        playlist_artwork.c.playlist_id == playlist_id)).all())
    db.execute(playlist_artwork.insert().values(
        playlist_id=playlist_id, artwork_id=artwork_id, display_order=order))
    db.commit()


@app.post("/upload/personal", response_model=ArtworkSchema)
async def upload_personal_photo(
    file: UploadFile = File(...),
    caption: Optional[str] = Form(None),
    date: Optional[str] = Form(None),
    playlist_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
):
    """Studio → My Photos: add one of the user's OWN photos to the local library. Unlike /upload, this
    deliberately skips the museum AI pipeline (the photo is never sent to a model — the privacy
    headline) and the review queue: it lands `approved` with `is_personal=True`. EXIF orientation is
    baked in so phone photos display upright; caption/date are optional (shown on a stripped placard).
    Links into the given playlist, or an auto-created "My Photos" playlist."""
    if not LIBRARY_DIR.exists():
        LIBRARY_DIR.mkdir(parents=True)
    raw = await file.read()

    def _decode_and_store():
        # A1: decode + EXIF-transpose + encode + disk write are blocking — run in a thread.
        with Image.open(io.BytesIO(raw)) as src:
            fmt = (src.format or "JPEG").upper()
            img = ImageOps.exif_transpose(src)   # bake phone orientation; drops the EXIF tag
        ext = {"PNG": ".png", "WEBP": ".webp"}.get(fmt, ".jpg")
        stem = Path(file.filename or "").stem
        base = "".join(c for c in (caption or stem or "photo") if c.isalnum() or c in " _-").strip()
        base = (base.replace(" ", "_")[:24] or "photo").lower()
        safe = f"personal_{base}{ext}"
        dest = LIBRARY_DIR / safe
        n = 1
        while dest.exists():
            safe = f"personal_{base}_{n}{ext}"; dest = LIBRARY_DIR / safe; n += 1
        if ext == ".jpg":
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(dest, quality=92)
        else:
            img.save(dest)
        return safe, *img.size

    try:
        safe, w, h = await run_in_threadpool(_decode_and_store)
    except Exception:
        raise HTTPException(400, detail="That file isn't a readable image.")

    art = ArtworkModel(
        filename=safe, original_width=w, original_height=h,
        crop_width=float(w), crop_height=float(h),
        title=(caption or None), date_display=(date or None),
        is_personal=True, status="approved",
    )
    db.add(art); db.commit(); db.refresh(art)
    warm_canvas_cache_async(art.id, safe)   # pre-render the display image so it's warm by display time

    pl = db.query(PlaylistModel).filter(PlaylistModel.id == playlist_id).first() if playlist_id else None
    if pl is None:
        pl = _get_or_create_playlist(db, PERSONAL_PLAYLIST_NAME, is_personal=True)
    _link_artwork_to_playlist(db, pl.id, art.id)
    return art


STUDIO_CAPTION_PROMPT = (
    "You are writing a short, warm caption for someone's personal photo — like the title in a family "
    "photo album or on the back of a postcard. Look at the image and write an evocative title of 3 to "
    "8 words that captures the PLACE, OCCASION, and MOOD. Name the location or event if it is provided "
    "below or clearly recognizable. Do NOT list objects or describe the scene clinically. "
    "Good: \"A Sunny Day at Bondi Beach\". Bad: \"A child holding a bucket and shovel on sand\". "
    "Return ONLY a JSON object: {\"caption\": \"<your title>\"}."
)


class CaptionRequest(BaseModel):
    hint: Optional[str] = None


@app.post("/api/studio/caption/{artwork_id}")
async def suggest_caption(artwork_id: int, payload: CaptionRequest, db: Session = Depends(get_db)):
    """Studio → My Photos: suggest an evocative, album-style caption for a personal photo via the
    configured vision model (opt-in). Returns the suggestion only — the user edits/accepts it in the
    UI. `model_is_local` tells the UI whether the photo stays on-device (a local Ollama/LM Studio
    model) or is sent to a cloud model, so the privacy note can be honest instead of a blanket warning."""
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art:
        raise HTTPException(404, detail="Artwork not found")
    img_path = LIBRARY_DIR / art.filename
    if not img_path.exists():
        raise HTTPException(404, detail="Image file missing")
    cfg = ai_client.get_ai_config(force=True)
    if not cfg["configured"]:
        raise HTTPException(400, detail="No AI model is configured. Add one in Settings → AI Engine, "
                                        "or type a caption yourself.")
    prompt = STUDIO_CAPTION_PROMPT
    hint = (payload.hint or "").strip()
    if hint:
        prompt += f" The user notes about this photo: \"{hint}\" — weave it in naturally."
    parts = [ai_client.text_part(prompt), ai_client.image_part(str(img_path))]
    try:
        resp = await asyncio.to_thread(
            ai_client.chat, "vision", [{"role": "user", "content": parts}], json_mode=True)
        caption = (ai_client.parse_json(resp).get("caption") or "").strip()
    except Exception as e:
        logger.warning(f"[Studio] caption generation failed: {e}")
        raise HTTPException(502, detail="Caption generation failed. Try again, or type one yourself.")
    if not caption:
        raise HTTPException(502, detail="The model didn't return a caption. Try again.")
    return {"caption": caption, "model_is_local": ai_client.is_local_base_url(cfg["base_url"])}


class PersonalPhotoUpdate(BaseModel):
    caption: Optional[str] = None
    date: Optional[str] = None


@app.patch("/api/studio/photo/{artwork_id}", response_model=ArtworkSchema)
async def update_personal_photo(artwork_id: int, payload: PersonalPhotoUpdate, db: Session = Depends(get_db)):
    """Studio → My Photos: save a personal photo's caption (title) and/or date. Restricted to personal
    photos so it can't be used to edit museum/catalog metadata."""
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art or not art.is_personal:
        raise HTTPException(404, detail="Personal photo not found")
    if payload.caption is not None:
        art.title = payload.caption.strip() or None
    if payload.date is not None:
        art.date_display = payload.date.strip() or None
    db.commit(); db.refresh(art)
    return art


@app.get("/api/studio/photos")
async def list_personal_photos(db: Session = Depends(get_db)):
    """Studio gallery: every personal photo (`is_personal`) grouped by album, so the user can find and
    re-edit a photo they uploaded earlier (Studio was previously upload-only). A photo with no album
    lands in an "Unfiled" group; one in several albums appears under each. Newest first within a group."""
    photos = db.query(ArtworkModel).filter(
        ArtworkModel.is_personal.is_(True)).order_by(ArtworkModel.id.desc()).all()
    names = {p.id: p.name for p in db.query(PlaylistModel).all()}
    by_art: dict[int, list[int]] = {}
    for pid, aid in db.execute(select(
            playlist_artwork.c.playlist_id, playlist_artwork.c.artwork_id)).all():
        by_art.setdefault(aid, []).append(pid)

    def _photo(a):
        return {"id": a.id, "title": a.title, "date_display": a.date_display,
                "focal_x": a.focal_x, "focal_y": a.focal_y, "filename": a.filename}

    albums: dict[int, dict] = {}
    unfiled: list[dict] = []
    for a in photos:
        pids = [pid for pid in by_art.get(a.id, []) if pid in names]
        if not pids:
            unfiled.append(_photo(a))
        for pid in pids:
            albums.setdefault(pid, {"playlist_id": pid, "name": names[pid], "photos": []})
            albums[pid]["photos"].append(_photo(a))
    out = sorted(albums.values(), key=lambda x: x["name"].lower())
    if unfiled:
        out.append({"playlist_id": None, "name": "Unfiled", "photos": unfiled})
    return {"albums": out, "count": len(photos)}


@app.get("/api/studio/albums")
async def list_personal_albums(db: Session = Depends(get_db)):
    """Personal albums for the My Photos chips — is_personal playlists only (Museum collections never
    appear here), including empty ones (so a freshly-created album shows immediately). Photo counts come
    from the playlist relationship. Sorted with the "My Photos" default first, then alphabetically."""
    albums = db.query(PlaylistModel).filter(PlaylistModel.is_personal.is_(True)).all()
    rows = [{"id": p.id, "name": p.name, "count": len(p.artworks),
             "is_default": p.name == PERSONAL_PLAYLIST_NAME} for p in albums]
    rows.sort(key=lambda r: (not r["is_default"], r["name"].lower()))
    return rows


class StudioAlbumPayload(BaseModel):
    name: str


@app.post("/api/studio/albums")
async def create_personal_album(payload: StudioAlbumPayload, db: Session = Depends(get_db)):
    """Create a personal album (a playlist flagged is_personal) from the My Photos chip row."""
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Album name required")
    if db.query(PlaylistModel).filter(PlaylistModel.name == name).first():
        raise HTTPException(status_code=400, detail=f"An album or collection named '{name}' already exists")
    pl = PlaylistModel(name=name, is_personal=True)
    db.add(pl); db.commit(); db.refresh(pl)
    return {"id": pl.id, "name": pl.name, "count": 0, "is_default": False}


@app.delete("/api/studio/albums/{album_id}")
async def delete_personal_album(album_id: int, db: Session = Depends(get_db)):
    """S2: delete a personal album (is_personal playlist). Photos are NOT deleted — they just become
    Unfiled; only the grouping goes. Scoped to personal albums (never a Museum collection) and refuses
    the default 'My Photos' album so gramps can't lose the home bucket."""
    pl = db.query(PlaylistModel).filter(PlaylistModel.id == album_id,
                                        PlaylistModel.is_personal.is_(True)).first()
    if not pl:
        raise HTTPException(status_code=404, detail="Personal album not found")
    if pl.name == PERSONAL_PLAYLIST_NAME:
        raise HTTPException(status_code=400, detail="The default My Photos album can't be deleted")
    db.delete(pl); db.commit()
    return {"status": "deleted"}


@app.get("/artworks/pending", response_model=List[ArtworkSchema])
async def get_pending_artworks(db: Session = Depends(get_db)):
    return db.query(ArtworkModel).filter(ArtworkModel.status == 'pending_review').all()

@app.patch("/artworks/{artwork_id}/approve", response_model=ArtworkSchema)
async def approve_artwork(artwork_id: int, data: ArtworkApproval, db: Session = Depends(get_db)):
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    art.title, art.agent_name, art.agent_role, art.creation_date, art.cultural_context, art.medium, art.date_display, art.description_narrative, art.tags, art.status = data.title, data.agent_name, data.agent_role, data.creation_date, data.cultural_context, data.medium, data.date_display, data.description_narrative, data.tags, 'approved'
    db.commit(); db.refresh(art); return art

@app.post("/artworks/approve-bulk")
async def bulk_approve_artworks(payload: ArtworkIds, db: Session = Depends(get_db)):
    """Bulk-publish Review-Queue items using their already-enriched stored values (multi-select
    Approve). Only flips pending_review → approved, so it can't accidentally re-touch published or
    in-flight items; ids that aren't pending are skipped. Per-item edits happen via the Edit landing."""
    arts = db.query(ArtworkModel).filter(
        ArtworkModel.id.in_(payload.artwork_ids or [-1]),
        ArtworkModel.status == 'pending_review',
    ).all()
    for art in arts:
        art.status = 'approved'
    db.commit(); return {"status": "approved", "count": len(arts)}

@app.patch("/artworks/{artwork_id}/metadata", response_model=ArtworkSchema)
async def update_artwork_metadata(artwork_id: int, data: ArtworkApproval, db: Session = Depends(get_db)):
    """Edit an already-approved artwork's placard metadata in place — the Edit landing's Save for
    museum/catalog works. Unlike /approve (the Review-Queue publish step), this does NOT touch status,
    so an approved piece stays approved. Personal photos edit via /api/studio/photo instead."""
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    art.title, art.agent_name, art.agent_role, art.creation_date, art.cultural_context, art.medium, art.date_display, art.description_narrative, art.tags = data.title, data.agent_name, data.agent_role, data.creation_date, data.cultural_context, data.medium, data.date_display, data.description_narrative, data.tags
    db.commit(); db.refresh(art); return art

@app.post("/api/curate/regenerate/{artwork_id}", response_model=ArtworkSchema)
async def regenerate_artwork_metadata(artwork_id: int, request: RegenerationRequest, db: Session = Depends(get_db)):
    """Manually triggers the AI pipeline with an optional human-in-the-loop hint."""
    updated_art = await process_artwork(artwork_id, db, user_hint=request.hint)
    if not updated_art:
        raise HTTPException(status_code=500, detail="AI Regeneration failed")
    return updated_art

@app.post("/api/curate/reenrich/{artwork_id}", response_model=ArtworkSchema)
async def reenrich_artwork(artwork_id: int, request: RegenerationRequest, db: Session = Depends(get_db)):
    """Sets artwork status back to pending and triggers AI re-enrichment."""
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)

    art.status = 'pending_review'
    db.commit()

    updated_art = await process_artwork(artwork_id, db, user_hint=request.hint)
    if not updated_art:      # A7: guard like the regenerate sibling — a None fails ArtworkSchema as an ugly 500
        raise HTTPException(status_code=500, detail="AI Re-enrichment failed")
    return updated_art

@app.post("/api/curate/batch-enrich")
async def batch_enrich(background_tasks: BackgroundTasks):
    """Triggers RAG enrichment for all approved artworks."""
    background_tasks.add_task(run_batch_enrich_bg)
    return {"status": "Batch enrichment started in background"}

@app.get("/api/discover/queue", response_model=List[DiscoveryQueueSchema])
async def get_discovery_queue(
    session_id: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Returns the list of pending art discoveries, optionally filtered by session."""
    query = db.query(DiscoveryQueueModel).filter(DiscoveryQueueModel.status == 'pending')
    if session_id:
        query = query.filter(DiscoveryQueueModel.search_session_id == session_id)
    return query.order_by(DiscoveryQueueModel.relevance_score.desc()).all()

@app.post("/api/discover/dispatch")
async def dispatch_discovery(request: DispatchRequest, background_tasks: BackgroundTasks):
    """Smart multi-source art discovery dispatch with query classification."""
    # Classify the query upfront to create a session with the right intent.
    # B1: thread the sync classify() (→ ai_client.chat, up to 90s) so it can't freeze the worker.
    intent = await asyncio.to_thread(_query_classifier.classify, request.search) if request.search else None
    limit = max(1, min(request.limit, 10))  # Clamp to 1–10

    # Create a search session for Load More support
    session = create_search_session(
        query=request.search or "",
        intent=intent,
        sources=request.sources,
        limit=limit
    )

    background_tasks.add_task(
        run_scouts_bg,
        query=request.search,
        sources=request.sources,
        session_id=session.session_id,
        limit=limit
    )
    return {
        "status": "Art scouts dispatched",
        "sources": request.sources,
        "search": request.search,
        "session_id": session.session_id,
        "intent": {
            "type": intent.query_type if intent else "freetext",
            "canonical": intent.canonical_name if intent else request.search,
        }
    }

@app.post("/api/discover/more")
async def load_more_discoveries(request: LoadMoreRequest, background_tasks: BackgroundTasks):
    """Fetches the next batch of results from an existing search session."""
    session = get_search_session(request.session_id)
    if not session:
        raise HTTPException(404, detail="Search session expired or not found. Please start a new search.")

    # Advance the offset
    session.offset += session.limit

    background_tasks.add_task(
        run_scouts_bg,
        query=session.query,
        sources=session.sources,
        session_id=session.session_id,
        limit=session.limit
    )
    return {
        "status": "Loading more results",
        "session_id": session.session_id,
        "offset": session.offset
    }

@app.post("/api/discover/approve/{item_id}")
async def approve_discovery(item_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Downloads approved discovery and adds to library."""
    item = db.query(DiscoveryQueueModel).filter(DiscoveryQueueModel.id == item_id).first()
    if not item: raise HTTPException(404)

    # 1. Download full-res image via the shared robust downloader (descriptive UA — Wikimedia/NASA
    #    reject the default httpx UA — plus 429 retry, redirects, and image validation).
    filename = f"scouted_{item_id}_{item.proposed_title.replace(' ', '_')[:50]}"
    _, filename, w, h = await _download_image_to_library(item.source_url, filename=filename)

    # 2. Add to database
    new_art = ArtworkModel(
        filename=filename,
        original_width=w, original_height=h,
        title=item.proposed_title,
        agent_name=item.proposed_artist,
        source_url=item.source_url,
        status='processing'
    )
    db.add(new_art)
    item.status = 'approved'
    db.commit()
    db.refresh(new_art)

    # 3. Enrich with RAG Curator
    background_tasks.add_task(run_rag_pipeline, new_art.id, item.context_hints)

    return {"status": "Art added and fully enriched", "artwork_id": new_art.id}

@app.post("/api/discover/reject/{item_id}")
async def reject_discovery(item_id: int, db: Session = Depends(get_db)):
    """Removes a discovery from the queue."""
    item = db.query(DiscoveryQueueModel).filter(DiscoveryQueueModel.id == item_id).first()
    if not item: raise HTTPException(404)
    item.status = 'rejected'
    db.commit()
    return {"status": "Rejected"}

@app.delete("/api/discover/history")
async def clear_rejected_history(db: Session = Depends(get_db)):
    """Deletes all rejected items from the discovery queue to free up the cache."""
    db.execute(delete(DiscoveryQueueModel).where(DiscoveryQueueModel.status == 'rejected'))
    db.commit()
    return {"status": "History cleared"}

@app.delete("/api/discover/orphans")
async def clear_orphaned_approvals(db: Session = Depends(get_db)):
    """Deletes discovery queue items that were 'approved' but have no active artwork entry."""
    approved_items = db.query(DiscoveryQueueModel).filter(DiscoveryQueueModel.status == 'approved').all()
    artworks = db.query(ArtworkModel.filename).filter(ArtworkModel.filename.like('scouted_%')).all()

    active_scout_ids = set()
    for (fname,) in artworks:
        parts = fname.split('_')
        if len(parts) >= 2 and parts[1].isdigit():
            active_scout_ids.add(int(parts[1]))

    orphans_deleted = 0
    for item in approved_items:
        if item.id not in active_scout_ids:
            db.delete(item)
            orphans_deleted += 1

    db.commit()
    return {"status": f"Successfully cleared {orphans_deleted} orphaned approvals"}

@app.delete("/api/discover/clear-pending")
async def clear_pending_discoveries(db: Session = Depends(get_db)):
    """Deletes all pending items from the discovery queue. Useful for fresh test runs."""
    result = db.execute(delete(DiscoveryQueueModel).where(DiscoveryQueueModel.status == 'pending'))
    db.commit()
    # Clear any active search sessions
    from scout import _search_sessions
    _search_sessions.clear()
    return {"status": f"Cleared {result.rowcount} pending discoveries"}

class FactoryResetRequest(BaseModel):
    confirm: str = ""


@app.post("/api/admin/factory-reset")
async def factory_reset(req: FactoryResetRequest, db: Session = Depends(get_db)):
    """
    Resets the app to factory state:
    - Keeps only seed artworks (is_seed=True)
    - Removes all non-seed artworks from DB and disk
    - Clears entire discovery queue (all statuses)
    - Clears playlist-artwork associations for deleted art
    - Clears search sessions

    H4: the "RESET" confirmation is enforced server-side, not just by the admin UI's dialog — a bare
    POST (accidental, scripted, or drive-by) must not be able to wipe the library.
    """
    if req.confirm != "RESET":
        raise HTTPException(400, detail='Confirmation required: POST {"confirm": "RESET"}.')

    # 1. Delete all discovery queue items (all statuses)
    discover_count = db.execute(delete(DiscoveryQueueModel)).rowcount

    # 2. Get non-seed artworks to delete their files
    non_seed_art = db.query(ArtworkModel).filter(ArtworkModel.is_seed != True).all()
    files_deleted = 0
    for art in non_seed_art:
        filepath = LIBRARY_DIR / art.filename
        # Handle both real files and symlinks
        if filepath.is_symlink() or filepath.exists():
            try:
                filepath.unlink()
                files_deleted += 1
            except Exception as e:
                logger.warning(f"[Factory Reset] Could not delete {filepath}: {e}")
        # Also clean up any playlist symlinks pointing to this file
        for pl_dir in ARTWORK_ROOT.iterdir():
            if pl_dir.is_dir() and not pl_dir.name.startswith('_'):
                pl_link = pl_dir / art.filename
                if pl_link.is_symlink() or pl_link.exists():
                    try: pl_link.unlink()
                    except Exception: pass

    # 3. Remove ALL artwork-playlist associations (both seed and non-seed)
    db.execute(playlist_artwork.delete())

    # 4. Delete non-seed artwork records from DB
    art_count = db.query(ArtworkModel).filter(ArtworkModel.is_seed != True).delete(synchronize_session='fetch')

    # 5. Delete seed artworks too so bootstrapper re-downloads on next start
    seed_art = db.query(ArtworkModel).filter(ArtworkModel.is_seed == True).all()
    for art in seed_art:
        filepath = LIBRARY_DIR / art.filename
        if filepath.is_symlink() or filepath.exists():
            try: filepath.unlink()
            except Exception: pass
        # Clean playlist symlinks for seed art too
        for pl_dir in ARTWORK_ROOT.iterdir():
            if pl_dir.is_dir() and not pl_dir.name.startswith('_'):
                pl_link = pl_dir / art.filename
                if pl_link.is_symlink() or pl_link.exists():
                    try: pl_link.unlink()
                    except Exception: pass
    seed_count = db.query(ArtworkModel).filter(ArtworkModel.is_seed == True).delete(synchronize_session='fetch')

    # 6. Clear search sessions
    from scout import _search_sessions
    _search_sessions.clear()

    # 7. Drop cached Canvas display derivatives (regenerated on demand from originals)
    if DERIVATIVES_DIR.exists():
        for d in DERIVATIVES_DIR.glob("*.jpg"):
            try: d.unlink()
            except OSError: pass

    db.commit()

    logger.info(f"[Factory Reset] Removed {art_count} + {seed_count} seed artworks, {files_deleted} files, {discover_count} queue items. Seeds will re-download on restart.")
    return {
        "status": "Factory reset complete. Restart the server to re-seed masterpieces.",
        "artworks_removed": art_count,
        "seed_artworks_removed": seed_count,
        "files_deleted": files_deleted,
        "queue_items_cleared": discover_count,
    }

@app.get("/artworks/{artwork_id}/thumbnail")
async def get_artwork_thumbnail(artwork_id: int, db: Session = Depends(get_db)):
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    path = LIBRARY_DIR / art.filename
    # A1: Pillow decode/resize/encode is blocking — thread it so a cold admin grid (dozens of concurrent
    # misses) doesn't serialize on the worker's event loop. Mirrors /display.jpg below.
    data = await run_in_threadpool(get_optimized_image, path, (400, 400), quality=70)
    return Response(content=data, media_type="image/jpeg")

@app.get("/artworks/{artwork_id}/preview")
async def get_artwork_preview(artwork_id: int, db: Session = Depends(get_db)):
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    path = LIBRARY_DIR / art.filename
    data = await run_in_threadpool(get_optimized_image, path, (1920, 1080), quality=85)
    return Response(content=data, media_type="image/jpeg")

@app.get("/artworks/{artwork_id}/display.jpg")
async def get_artwork_display(artwork_id: int, db: Session = Depends(get_db)):
    """Resolution-capped image the Canvas loads instead of the full-res original
    (see render_canvas_image). Ends in .jpg → inherits the immutable media cache tier;
    the ?v=<mtime> query the caller appends busts that cache when the source changes."""
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    path = LIBRARY_DIR / art.filename
    if not path.exists(): raise HTTPException(404)
    data = await run_in_threadpool(render_canvas_image, path, art.id)
    return Response(content=data, media_type="image/jpeg")

@app.get("/art/{artwork_id}", response_class=HTMLResponse)
async def artwork_detail_page(artwork_id: int, db: Session = Depends(get_db)):
    """Server-hosted 'Learn More' page the placard QR points at — works offline (no Google hand-off)."""
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art:
        return HTMLResponse("<body style='font-family:sans-serif;background:#0b1020;color:#e2e8f0;padding:40px'>"
                            "<h1>Artwork not found</h1></body>", status_code=404)
    e = html.escape
    if art.is_personal:
        # Personal photo: caption + optional date only — no artist/medium/culture/tags/source jargon.
        title = e(art.title or "My Photo")
        artist_line = e(art.date_display or art.creation_date or "")
        meta_bits = desc = tag_html = source = ""
    else:
        title = e(strip_markdown(art.title or "Untitled"))
        role = e(art.agent_role) if art.agent_role and art.agent_role != "Artist" else ""
        date = e(art.date_display or art.creation_date or "")
        artist_line = e(art.agent_name or "Unknown artist") + (f" · {role}" if role else "") + (f" · {date}" if date else "")
        meta_bits = " · ".join(b for b in [e(art.cultural_context or ""), e(art.medium or "")] if b)
        desc = e(strip_markdown(art.description_narrative or ""))
        tag_html = "".join(f"<span class=tag>{e(t.strip())}</span>" for t in (art.tags or "").split(",") if t.strip())
        source = (f"<a class=source href='{e(art.source_url)}' target=_blank rel=noopener>View original source ↗</a>"
                  if art.source_url else "")
    return HTMLResponse(f"""<!DOCTYPE html><html lang=en><head>
<meta charset=UTF-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>{title} — Screen Docent</title><style>
 /* Canonical palette inlined (kept self-contained so this public landing works offline). */
 :root {{ color-scheme: dark; --bg:#0f172a; --surface:#1e293b; --inset:#0f172a; --border:#334155; --text:#f1f5f9; --muted:#94a3b8; --accent:#3b82f6; }}
 body {{ margin:0; background:var(--bg); color:var(--text); font-family:'Inter',system-ui,-apple-system,sans-serif; line-height:1.6; }}
 .wrap {{ max-width:760px; margin:0 auto; padding:24px 20px 60px; }}
 img.hero {{ width:100%; border-radius:14px; background:var(--inset); display:block; margin-bottom:24px; box-shadow:0 10px 40px rgba(0,0,0,.5); }}
 h1 {{ font-size:1.8rem; margin:0 0 6px; }}
 .artist {{ font-size:1.1rem; color:#cbd5e1; margin:0 0 4px; }}
 .meta {{ color:var(--muted); font-size:.92rem; margin:0 0 20px; }}
 .desc {{ font-size:1.02rem; }}
 .tags {{ margin-top:22px; display:flex; flex-wrap:wrap; gap:8px; }}
 .tag {{ background:var(--surface); border:1px solid var(--border); color:#cbd5e1; padding:4px 12px; border-radius:20px; font-size:.8rem; }}
 .source {{ display:inline-block; margin-top:24px; color:var(--accent); text-decoration:none; }}
 .brand {{ margin-top:40px; color:#475569; font-size:.78rem; display:flex; align-items:center; gap:8px; }}
 .brand img {{ height:20px; opacity:.7; }}
</style></head><body><div class=wrap>
 <img class=hero src="/artworks/{art.id}/preview" alt="{title}">
 <h1>{title}</h1>
 <p class=artist>{artist_line}</p>
 {f'<p class=meta>{meta_bits}</p>' if meta_bits else ''}
 {f'<p class=desc>{desc}</p>' if desc else ''}
 {f'<div class=tags>{tag_html}</div>' if tag_html else ''}
 {source}
 <div class=brand><img src="/logo.svg" alt=""> Presented by Screen Docent</div>
</div></body></html>""")

class CropPayload(BaseModel):
    crop_x: float = 0.0
    crop_y: float = 0.0
    crop_width: float = 0.0
    crop_height: float = 0.0
    focal_x: Optional[float] = None
    focal_y: Optional[float] = None

@app.patch("/artworks/{artwork_id}/crop", response_model=ArtworkSchema)
async def update_artwork_crop(artwork_id: int, payload: CropPayload, db: Session = Depends(get_db)):
    """Persist a manual crop rectangle (original pixels) from the admin Cropper, plus optionally the
    normalized focal point (the Ken Burns / e-ink framing anchor). The admin crop modal calls this;
    it was previously missing, so manual crop-saves silently failed."""
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art:
        raise HTTPException(404)
    art.crop_x = payload.crop_x
    art.crop_y = payload.crop_y
    art.crop_width = payload.crop_width
    art.crop_height = payload.crop_height
    if payload.focal_x is not None:
        art.focal_x = min(1.0, max(0.0, payload.focal_x))
    if payload.focal_y is not None:
        art.focal_y = min(1.0, max(0.0, payload.focal_y))
    db.commit(); db.refresh(art)
    return art

def _wipe_artwork(db: Session, art: ArtworkModel) -> None:
    """Delete an artwork's library file + DB row (playlist associations cascade). Shared by the single
    and bulk delete paths so they can't drift."""
    f_path = LIBRARY_DIR / art.filename
    if f_path.is_symlink() or f_path.exists():
        try: f_path.unlink()
        except Exception as e: logger.warning(f"[Delete] could not unlink {f_path}: {e}")
    db.delete(art)

@app.delete("/artworks/{artwork_id}")
async def permanent_delete_artwork(artwork_id: int, db: Session = Depends(get_db)):
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    _wipe_artwork(db, art); db.commit(); return {"status": "wiped"}

@app.post("/artworks/delete")
async def bulk_delete_artworks(payload: ArtworkIds, db: Session = Depends(get_db)):
    """Bulk permanent delete (multi-select Delete in the Library). POST (not DELETE) so the id list
    rides in the body without colliding with DELETE /artworks/{id}. Skips ids that no longer exist."""
    arts = db.query(ArtworkModel).filter(ArtworkModel.id.in_(payload.artwork_ids or [-1])).all()
    for art in arts:
        _wipe_artwork(db, art)
    db.commit(); return {"status": "wiped", "count": len(arts)}

@app.get("/api/displays/{display_id}/preferred-playlist")
async def get_preferred_playlist(display_id: str, db: Session = Depends(get_db)):
    """Which playlist a freshly-loaded display (no ?playlist= given) should show. Precedence:
    last-played for THIS display → the global `default_playlist` fallback → null (Canvas then picks the
    first non-empty). Only ever returns a playlist that still exists and has art."""
    last = db.query(SettingsModel).filter(SettingsModel.setting_key == f"last_playlist:{display_id}").first()
    default = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    name = (_playlist_name_if_playable(db, last.setting_value if last else None)
            or _playlist_name_if_playable(db, default.setting_value if default else None))
    return {"playlist": name}

class DefaultPlaylistPayload(BaseModel):
    default_playlist: Optional[str] = None

@app.get("/api/settings/default-playlist")
async def get_default_playlist(db: Session = Depends(get_db)):
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    return {"default_playlist": row.setting_value if row else None}

@app.post("/api/settings/default-playlist")
async def set_default_playlist(payload: DefaultPlaylistPayload, db: Session = Depends(get_db)):
    """Pin the fallback playlist a display boots to when it has no last-played history (e.g. a brand-new
    wall display). Empty string clears it. Validated against existing playlists."""
    name = (payload.default_playlist or "").strip()
    if name and not db.query(PlaylistModel).filter(PlaylistModel.name == name).first():
        raise HTTPException(400, detail=f"No playlist named '{name}'")
    _upsert_setting(db, "default_playlist", name)
    db.commit()
    return {"default_playlist": name}

# --- R1-F2: Night & Quiet Hours (clock-driven brightness/warmth + quiet-hours panel power) ----------
# Gentle defaults, warm-shift ON, quiet-hours panel-off OFF (opt-in) so nothing blanks unexpectedly.
# One global schedule for v1; the resolver takes a display_id so per-display overrides can layer in later
# (dev-rule #4 hierarchy). The Canvas applies a GPU-cheap CSS overlay; the appliance drives HDMI-CEC.
# SCHEDULE_SETTING_KEY, DEFAULT_SCHEDULE now live in core/settings_util.py (imported above).


def _parse_hhmm(value: str, fallback: int = 0) -> int:
    """'HH:MM' -> minutes since midnight (0..1439); tolerant, clamps, falls back on garbage."""
    try:
        h, m = str(value).split(":")
        return (int(h) % 24) * 60 + (int(m) % 60)
    except (ValueError, AttributeError):
        return fallback


def _cyc_len(a: int, b: int) -> int:
    """Clockwise minute span from a to b on a 24h dial (a==b -> full 1440-min day is treated as 0)."""
    return (b - a) % 1440


def _cyc_in(t: int, a: int, b: int) -> bool:
    """Is minute t within the clockwise window [a, b) — handles windows that wrap past midnight."""
    span = _cyc_len(a, b)
    return span > 0 and (t - a) % 1440 < span


def _cyc_frac(t: int, a: int, b: int) -> float:
    """Fraction (0..1) of the clockwise window [a, b) elapsed at minute t (wrap-safe)."""
    span = _cyc_len(a, b)
    return 0.0 if span == 0 else ((t - a) % 1440) / span


def resolve_schedule_state(schedule: dict, now: datetime) -> dict:
    """Pure: given the schedule config + a wall-clock time, return what the display should look like NOW.

    Returns {enabled, brightness (0.1..1), warmth (0..1), quiet (bool), quiet_mode}. 'night factor' n
    ramps 0 (day) -> 1 (night) across the evening window, holds at 1 overnight, and ramps back down over
    the morning window; brightness/warmth interpolate on n. Disabled -> fully neutral, no quiet.
    """
    s = {**DEFAULT_SCHEDULE, **(schedule or {})}
    if not s.get("enabled", True):
        return {"enabled": False, "brightness": 1.0, "warmth": 0.0, "quiet": False, "quiet_mode": s.get("quiet_mode", "cec")}

    t = now.hour * 60 + now.minute
    day_start = _parse_hhmm(s["day_start"], 480)
    evening = _parse_hhmm(s["evening_start"], 1200)
    night = _parse_hhmm(s["night_start"], 1350)
    morning = _parse_hhmm(s["morning_start"], 390)

    if _cyc_in(t, day_start, evening):
        n = 0.0
    elif _cyc_in(t, evening, night):
        n = _cyc_frac(t, evening, night)          # rising: day -> night
    elif _cyc_in(t, night, morning):
        n = 1.0                                    # night plateau (wraps midnight)
    elif _cyc_in(t, morning, day_start):
        n = 1.0 - _cyc_frac(t, morning, day_start)  # falling: night -> day
    else:
        n = 0.0                                    # windows didn't tile (misconfig) — default to day

    n = max(0.0, min(1.0, n))
    day_b = float(s["day_brightness"])
    night_b = float(s["night_brightness"])
    brightness = round(day_b + (night_b - day_b) * n, 4)
    warmth = round(float(s["night_warmth"]) * n, 4)

    quiet = bool(s.get("quiet_enabled")) and _cyc_in(
        t, _parse_hhmm(s["quiet_start"], 1410), _parse_hhmm(s["quiet_end"], 420))

    return {"enabled": True, "brightness": brightness, "warmth": warmth,
            "quiet": quiet, "quiet_mode": s.get("quiet_mode", "cec")}


# _load_schedule now lives in core/settings_util.py (imported above).


class DisplaySchedulePayload(BaseModel):
    enabled: Optional[bool] = None
    day_brightness: Optional[float] = None
    night_brightness: Optional[float] = None
    night_warmth: Optional[float] = None
    evening_start: Optional[str] = None
    night_start: Optional[str] = None
    morning_start: Optional[str] = None
    day_start: Optional[str] = None
    quiet_enabled: Optional[bool] = None
    quiet_start: Optional[str] = None
    quiet_end: Optional[str] = None
    quiet_mode: Optional[str] = None


_HHMM_RE = re.compile(r"^\d{1,2}:\d{2}$")


@app.get("/api/settings/display-schedule")
async def get_display_schedule(db: Session = Depends(get_db)):
    return _load_schedule(db)


@app.post("/api/settings/display-schedule")
async def set_display_schedule(payload: DisplaySchedulePayload, db: Session = Depends(get_db)):
    """Merge the given fields over the current schedule, validate, and persist as JSON."""
    merged = _load_schedule(db)
    for k, v in payload.model_dump(exclude_none=True).items():
        merged[k] = v
    # Validate ranges/formats so a bad value can't wedge the resolver or the Canvas overlay.
    for bkey in ("day_brightness", "night_brightness"):
        if not (0.1 <= float(merged[bkey]) <= 1.0):
            raise HTTPException(400, detail=f"{bkey} must be between 0.1 and 1.0")
    if not (0.0 <= float(merged["night_warmth"]) <= 1.0):
        raise HTTPException(400, detail="night_warmth must be between 0.0 and 1.0")
    for tkey in ("evening_start", "night_start", "morning_start", "day_start", "quiet_start", "quiet_end"):
        if not _HHMM_RE.match(str(merged[tkey])):
            raise HTTPException(400, detail=f"{tkey} must be HH:MM")
    if merged["quiet_mode"] not in ("cec", "blackout"):
        raise HTTPException(400, detail="quiet_mode must be 'cec' or 'blackout'")
    _upsert_setting(db, SCHEDULE_SETTING_KEY, json.dumps(merged))
    db.commit()
    return merged


@app.get("/api/displays/{display_id}/schedule-state")
async def get_schedule_state(display_id: str, now: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """The display's current brightness/warmth/quiet, resolved server-side from the wall clock. The Canvas
    polls this (~60s) and applies a CSS overlay; the appliance CEC timer polls it for panel power. `now`
    (HH:MM) overrides the clock for testing / filming the warm-shift time-lapse without waiting for night."""
    when = datetime.now()
    if now:
        m = _parse_hhmm(now, -1)
        if m < 0 or not _HHMM_RE.match(now):
            raise HTTPException(400, detail="now must be HH:MM")
        when = when.replace(hour=m // 60, minute=m % 60)
    return resolve_schedule_state(_load_schedule(db), when)


@app.get("/next-image")
async def get_next_image(
    playlist_name: str,
    shuffle: Optional[bool] = Query(None),
    display_id: str = Query("default"),
    direction: int = Query(1),
    db: Session = Depends(get_db)
):
    """Stateful next-image selection — thin route over core.playback.select_next_image."""
    return await select_next_image(playlist_name, shuffle, display_id, direction, db)


@app.get("/display/{display_id}/current.{ext}")
async def get_display_image(
    display_id: str,
    ext: str,
    playlist: Optional[str] = Query(None),
    w: int = Query(1600, ge=16, le=4096),
    h: int = Query(1200, ge=16, le=4096),
    palette: str = Query("spectra6"),
    fit: str = Query("cover"),
    shuffle: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Track B: stateless pull-on-wake image for e-ink / BYOS frames.

    Reuses /next-image's selection (advancing the same bag-shuffle), then renders
    the chosen artwork cropped to w x h and Floyd–Steinberg-dithered to the device
    palette. Returns the bytes plus an `X-Refresh-After` header (the playlist's
    display_time) so the frame knows how long to deep-sleep. No WebSocket, no JS.
    """
    ext = ext.lower()
    if ext not in VALID_FORMATS:
        raise HTTPException(404, detail="Use .png or .bmp")
    if palette not in PALETTES:
        raise HTTPException(400, detail=f"Unknown palette. Options: {', '.join(PALETTES)}")

    # Playlist binding is stateless (v1): explicit ?playlist=, else the first one.
    if not playlist:
        first = db.query(PlaylistModel).order_by(PlaylistModel.id).first()
        if not first:
            raise HTTPException(404, detail="No playlists exist")
        playlist = first.name

    # Reuse the canonical selection brain (advances state once per fetch).
    info = await select_next_image(
        playlist_name=playlist, shuffle=shuffle, display_id=display_id, direction=1, db=db
    )

    art = db.query(ArtworkModel).filter(ArtworkModel.id == info["metadata"]["id"]).first()
    if not art:
        raise HTTPException(404, detail="Selected artwork not found")
    path = LIBRARY_DIR / art.filename
    if not path.exists():
        raise HTTPException(404, detail="Artwork file missing")

    try:
        # A1: crop + enhance + Floyd–Steinberg dither + encode is heavy and blocking — thread it so an
        # e-ink cache miss doesn't stall the worker loop (frame_push threads its sibling render likewise).
        data = await run_in_threadpool(render_for_epaper, path, w, h, palette=palette, fit=fit,
                                       focal=(art.focal_x, art.focal_y), fmt=ext)
    except Exception as e:
        logger.error(f"[epaper] render failed for {path.name}: {e}", exc_info=True)
        raise HTTPException(500, detail="Render failed")

    touch_active_display(db, display_id)

    return Response(
        content=data,
        media_type=media_type_for(ext),
        headers={
            "X-Refresh-After": str(info["display_time"]),
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )

# -----------------------------------------------------------------------------
# 4. WebSocket & Remote Control
# -----------------------------------------------------------------------------
@app.get("/remote")
async def get_remote_page():
    return FileResponse(STATIC_DIR / "remote.html")

@app.get("/api/remote/displays")
async def get_active_displays(db: Session = Depends(get_db)):
    """Active displays (seen in the last 15s), each with what it's currently showing so the Remote can
    render a 'now showing' panel + highlight the active collection. Shape: {display_id, playlist, artwork}."""
    cutoff = datetime.now(UTC) - timedelta(seconds=15)
    displays = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.last_seen_at > cutoff).all()
    return [_display_now_playing(db, d) for d in displays]


@app.get("/api/displays/{display_id}/now-playing")
async def get_display_now_playing(display_id: str, db: Session = Depends(get_db)):
    """What one display is currently showing (artwork + collection). Powers the Remote's 'now showing'
    panel; polled alongside the display list. artwork is null until the display has served a frame."""
    cutoff = datetime.now(UTC) - timedelta(seconds=15)
    row = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()
    if not row:
        return {"display_id": display_id, "active": False, "playlist": None, "artwork": None}
    active = db.query(ActiveDisplayModel).filter(
        ActiveDisplayModel.display_id == display_id,
        ActiveDisplayModel.last_seen_at > cutoff).first() is not None
    return {**_display_now_playing(db, row), "active": active}

@app.get("/api/health/host")
async def get_host_health(db: Session = Depends(get_db)):
    """Device Health console data: this box's host metrics + the displays it currently serves.

    All-in-one only — returns 404 on a generic/MS-01 server or thin-client topology (where the
    server isn't running ON the managed device), so the admin UI keeps the Devices tab hidden there.
    Compute-on-request: the readers are microseconds of /proc + /sys reads, so no DB table or
    background collector is needed."""
    if not config.IS_APPLIANCE:
        raise HTTPException(status_code=404, detail="host metrics unavailable")
    cutoff = datetime.now(UTC) - timedelta(seconds=15)
    displays = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.last_seen_at > cutoff).all()
    return {
        "available": True,
        "host": host_health.collect(),
        "displays": [
            {"display_id": d.display_id, "last_seen_at": d.last_seen_at.isoformat(),
             "playlist": d.current_playlist, "artwork": _now_playing_artwork(db, d.current_artwork_id)}
            for d in displays
        ],
    }

# --- Appliance update bridge (all-in-one only) -------------------------------------------------
# The container is unprivileged and cannot run git/docker/reboot. So a GUI action just writes a
# request file into the ./data bind mount; a root systemd .path unit notices it and runs the
# whitelisted host helper `sd-update`, which writes status back here for the UI to poll. The web
# app never gains host privileges.
ALLOWED_UPDATE_ACTIONS = {"update-app", "update-scripts", "reboot"}
_appliance_token_warned = False


class ApplianceUpdateRequest(BaseModel):
    action: str


@app.post("/api/appliance/update")
async def appliance_update(req: ApplianceUpdateRequest, request: Request,
                           x_appliance_token: Optional[str] = Header(None)):
    global _appliance_token_warned
    if not config.IS_APPLIANCE:
        raise HTTPException(status_code=403, detail="appliance update bridge not enabled")
    if req.action not in ALLOWED_UPDATE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action: {req.action}")
    # H6: this is the highest-consequence action (host git reset+rebuild / reboot). The cross-origin
    # guard already blocks a hostile browser tab; the shared-secret token additionally closes the
    # no-Origin path (curl / any other LAN device). Accept EITHER a valid token OR a trusted
    # (same-origin) Origin, so the same-origin admin GUI keeps working without holding the secret.
    if config.APPLIANCE_UPDATE_TOKEN:
        token_ok = bool(x_appliance_token) and secrets.compare_digest(
            x_appliance_token, config.APPLIANCE_UPDATE_TOKEN)
        origin_ok = _origin_allowed(request.headers.get("origin", ""), request.headers.get("host", ""))
        if not (token_ok or origin_ok):
            raise HTTPException(status_code=403, detail="appliance update requires a valid token")
    elif not _appliance_token_warned:
        logger.warning("SD_APPLIANCE_UPDATE_TOKEN is unset — /api/appliance/update is gated only by the "
                       "cross-origin guard. Set it to require a shared secret from non-browser callers.")
        _appliance_token_warned = True
    nonce = secrets.token_hex(8)
    config.APPLIANCE_DIR.mkdir(parents=True, exist_ok=True)
    # Write the status FIRST (so the .path trigger always finds a status), then the request.
    status = {"state": "queued", "action": req.action, "nonce": nonce,
              "message": "queued", "log_tail": []}
    (config.APPLIANCE_DIR / "status.json").write_text(json.dumps(status))
    request = {"action": req.action, "requested_at": datetime.now(UTC).isoformat(), "nonce": nonce}
    (config.APPLIANCE_DIR / "request.json").write_text(json.dumps(request))
    logger.info(f"Appliance update queued: {req.action} (nonce {nonce})")
    return {"status": "queued", "nonce": nonce}


@app.get("/api/appliance/update/status")
async def appliance_update_status():
    if not config.IS_APPLIANCE:
        raise HTTPException(status_code=403, detail="appliance update bridge not enabled")
    status_file = config.APPLIANCE_DIR / "status.json"
    if not status_file.exists():
        return {"state": "idle"}
    try:
        return json.loads(status_file.read_text())
    except (OSError, json.JSONDecodeError):
        return {"state": "idle"}

@app.post("/api/remote/change")
async def remote_change_playlist(request: RemoteChangeRequest, db: Session = Depends(get_db)):
    """Targeted command to change a playlist, mode, or trigger navigation on a specific display."""
    logger.info(f"Targeted Remote Command: {request.target_display} -> {request.action}")

    payload = {"action": request.action}
    if request.playlist:
        payload["playlist"] = request.playlist
    if request.mode:
        payload["mode"] = request.mode

    # Phase 5: Persist command to DB to bridge across worker processes
    cmd = RemoteCommandModel(
        target_display=request.target_display,
        action=request.action,
        payload=json.dumps(payload)
    )
    db.add(cmd)
    db.commit()

    return {"status": "command_queued"}

@app.websocket("/ws/{display_id}")
async def websocket_endpoint(websocket: WebSocket, display_id: str):
    """Handles targeted display connections with multi-worker synchronization."""
    # H5: WebSockets are not covered by CORS, so a hostile page could otherwise open this socket
    # (CSWSH) to observe/redirect a display. Reject a cross-origin handshake; a browser always sends
    # Origin, while native kiosk/CDP clients send none (allowed — the accepted LAN-presence model).
    origin = websocket.headers.get("origin", "")
    if origin and not _origin_allowed(origin, websocket.headers.get("host", "")):
        await websocket.close(code=1008)
        return
    await manager.connect(websocket, display_id)

    async def heartbeat():
        """Updates the active_displays table to signify this display is alive on this worker."""
        while True:
            try:
                with SessionLocal() as db:
                    display = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()
                    if display:
                        display.last_seen_at = datetime.now(UTC)
                    else:
                        display = ActiveDisplayModel(display_id=display_id)
                        db.add(display)
                    db.commit()
            except Exception as e:
                logger.error(f"Heartbeat error for {display_id}: {e}", exc_info=True)
            await asyncio.sleep(5)

    async def command_poller():
        """Polls the remote_commands table for actions targeting this specific display."""
        while True:
            try:
                with SessionLocal() as db:
                    cmds = db.query(RemoteCommandModel).filter(RemoteCommandModel.target_display == display_id).all()
                    for cmd in cmds:
                        logger.info(f"Relaying remote command to {display_id}: {cmd.action}")
                        await manager.send_personal_message(json.loads(cmd.payload), display_id)
                        db.delete(cmd)
                    db.commit()
            except Exception as e:
                logger.error(f"Command poller error for {display_id}: {e}", exc_info=True)
            await asyncio.sleep(1)

    # Start sync workers
    heartbeat_task = asyncio.create_task(heartbeat())
    poller_task = asyncio.create_task(command_poller())

    try:
        while True:
            # A frame sent up this socket is echoed only to sockets on THIS display_id — never
            # broadcast to every screen (H5: that let one anonymous client inject to all displays).
            data = await websocket.receive_json()
            await manager.send_personal_message(data, display_id)
    except WebSocketDisconnect:
        manager.disconnect(websocket, display_id)
    except Exception as e:
        logger.error(f"WebSocket error on '{display_id}': {e}", exc_info=True)
        manager.disconnect(websocket, display_id)
    finally:
        heartbeat_task.cancel()
        poller_task.cancel()
        # Clean up heartbeat from DB immediately on clean disconnect
        with SessionLocal() as db:
            db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).delete()
            db.commit()

# -----------------------------------------------------------------------------
# 4.5 Settings (API Keys)
# -----------------------------------------------------------------------------
@app.get("/api/settings/keys")
async def get_api_keys(db: Session = Depends(get_db)):
    """Returns a map of which API keys are unlocked."""
    settings = db.query(SettingsModel).all()
    # Check for presence of keys
    return {
        "harvard": any(s.setting_key == "harvard_api_key" for s in settings),
        "smithsonian": any(s.setting_key == "smithsonian_api_key" for s in settings),
        "europeana": any(s.setting_key == "europeana_api_key" for s in settings)
    }

class TelemetryHeartbeat(BaseModel):
    artwork_id: int
    display_time_sec: int
    skipped: bool

@app.post("/api/telemetry/heartbeat")
def record_telemetry(payload: TelemetryHeartbeat, db: Session = Depends(get_db)):
    """
    Phase 6: Ingests display metrics from Canvas clients.
    """
    artwork = db.query(ArtworkModel).filter(ArtworkModel.id == payload.artwork_id).first()
    if not artwork:
        raise HTTPException(status_code=404, detail="Artwork not found")

    # Update raw telemetry
    artwork.total_display_time += payload.display_time_sec
    if payload.skipped:
        artwork.skip_count += 1

    # Phase 6 Director Affinity Calculation (v1)
    # This is a naive calculation that will be evolved.
    # Base is 1.0.
    # Skipping heavily penalizes (-0.1).
    # Natural display slightly rewards (+0.05 per 30s).
    if payload.skipped:
        artwork.affinity_score = max(0.1, artwork.affinity_score - 0.1)
    else:
        intervals = payload.display_time_sec / 30.0
        artwork.affinity_score = min(5.0, artwork.affinity_score + (0.05 * intervals))

    db.commit()
    return {"status": "ok", "affinity": artwork.affinity_score}

@app.post("/api/settings/keys/{source}")
async def verify_and_save_api_key(source: str, payload: dict, db: Session = Depends(get_db)):
    """Validates an API key against the source museum backend and persists it."""
    key = payload.get("api_key")
    if not key: raise HTTPException(400, "api_key payload is required.")

    try:
        async with httpx.AsyncClient() as client:
            if source == "harvard":
                resp = await client.get(f"https://api.harvardartmuseums.org/object?apikey={key}&size=1", timeout=15)
                if resp.status_code != 200: raise Exception("Harvard API rejected the key.")
                db_key = "harvard_api_key"
            elif source == "smithsonian":
                resp = await client.get(f"https://api.si.edu/openaccess/api/v1.0/search?q=art&api_key={key}&rows=1", timeout=15)
                if resp.status_code != 200: raise Exception("Smithsonian API rejected the key.")
                db_key = "smithsonian_api_key"
            elif source == "europeana":
                resp = await client.get(f"https://api.europeana.eu/record/v2/search.json?wskey={key}&query=*&rows=1", timeout=15)
                if resp.status_code != 200: raise Exception("Europeana API rejected the key.")
                db_key = "europeana_api_key"
            else:
                raise HTTPException(400, f"Unsupported museum target: {source}")
    except HTTPException:
        raise   # A6: don't re-wrap a deliberate 400 (unsupported source) as a 401 "Validation Failed"
    except Exception as e:
        raise HTTPException(401, detail=f"Validation Failed: {str(e)}")

    setting = db.query(SettingsModel).filter(SettingsModel.setting_key == db_key).first()
    if setting:
        setting.setting_value = key
    else:
        setting = SettingsModel(setting_key=db_key, setting_value=key)
        db.add(setting)
    db.commit()
    return {"status": "success", "source": source}

# -----------------------------------------------------------------------------
# 4.6 AI Engine (model provider configuration)
# -----------------------------------------------------------------------------
# _upsert_setting now lives in core/settings_util.py (imported above).

@app.get("/api/settings/ai")
async def get_ai_settings(db: Session = Depends(get_db)):
    """Returns the current AI engine config (never the raw key) + provider presets for the UI."""
    rows = {
        s.setting_key: s.setting_value
        for s in db.query(SettingsModel)
        .filter(SettingsModel.setting_key.in_(ai_client.AI_SETTING_KEYS))
        .all()
    }
    cfg = ai_client.get_ai_config(force=True)
    return {
        "provider": rows.get("ai_provider", ai_client.DEFAULT_PROVIDER),
        "base_url": rows.get("ai_base_url", ""),
        "model": rows.get("ai_model", ""),
        "model_fast": rows.get("ai_model_fast", ""),
        "temperature": rows.get("ai_temperature", ""),
        "has_key": cfg["configured"],
        "key_source": "db" if rows.get("ai_api_key") else ("env" if cfg["configured"] else "none"),
        "model_is_local": ai_client.is_local_base_url(cfg["base_url"]),
        "presets": {
            k: {
                "label": v["label"],
                "base_url": v["base_url"],
                "models": v["models"],
                "oauth": v.get("oauth", False),
                "key_optional": v.get("key_optional", False),
                "key_url": v.get("key_url", ""),
            }
            for k, v in ai_client.PRESETS.items()
        },
    }

class AISettingsPayload(BaseModel):
    model_config = {"protected_namespaces": ()}  # allow "model"/"model_fast" field names
    provider: str
    base_url: Optional[str] = ""
    api_key: Optional[str] = None  # blank/omitted ⇒ keep the existing stored key
    model: str
    model_fast: Optional[str] = ""
    temperature: Optional[str] = ""

@app.post("/api/settings/ai")
async def save_ai_settings(payload: AISettingsPayload, db: Session = Depends(get_db)):
    """Validates a candidate AI config against the live endpoint, then persists it."""
    provider = payload.provider
    if provider not in ai_client.PRESETS:
        raise HTTPException(400, f"Unknown provider: {provider}")
    if not payload.model:
        raise HTTPException(400, "A model name is required.")

    base_url = (payload.base_url or ai_client.PRESETS[provider]["base_url"]).rstrip("/")
    existing = db.query(SettingsModel).filter(SettingsModel.setting_key == "ai_api_key").first()
    api_key = (
        (payload.api_key or "").strip()
        or (existing.setting_value if existing else "")
        or os.getenv("GEMINI_API_KEY", "")
    )
    key_optional = ai_client.PRESETS[provider].get("key_optional", False)
    if not api_key and not key_optional:
        raise HTTPException(400, "An API key is required for this provider.")

    # Validate against the live endpoint before persisting (mirrors the museum-key flow).
    try:
        await asyncio.to_thread(ai_client.validate_config, provider, base_url, api_key, payload.model)
    except Exception as e:
        raise HTTPException(401, detail=f"Validation failed: {str(e)}")

    _upsert_setting(db, "ai_provider", provider)
    _upsert_setting(db, "ai_base_url", base_url)
    if api_key:
        _upsert_setting(db, "ai_api_key", api_key)
    _upsert_setting(db, "ai_model", payload.model)
    _upsert_setting(db, "ai_model_fast", (payload.model_fast or "").strip())
    _upsert_setting(db, "ai_temperature", (payload.temperature or "").strip())
    db.commit()
    ai_client.invalidate_config_cache()
    return {"status": "success", "provider": provider, "model": payload.model}

@app.get("/api/settings/ai/oauth/start")
async def ai_oauth_start(callback_url: str, challenge: str):
    """Assembles the OpenRouter authorization URL (PKCE). The client holds the code_verifier."""
    from urllib.parse import urlencode
    params = urlencode({
        "callback_url": callback_url,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return {"auth_url": f"https://openrouter.ai/auth?{params}"}

class OAuthExchangePayload(BaseModel):
    code: str
    verifier: str

@app.post("/api/settings/ai/oauth/exchange")
async def ai_oauth_exchange(payload: OAuthExchangePayload, db: Session = Depends(get_db)):
    """Exchanges an OpenRouter auth code (+ PKCE verifier) for an API key and saves it."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/auth/keys",
                json={
                    "code": payload.code,
                    "code_verifier": payload.verifier,
                    "code_challenge_method": "S256",
                },
                timeout=20,
            )
        if resp.status_code != 200:
            raise Exception(resp.text[:200])
        key = resp.json().get("key")
        if not key:
            raise Exception("No key returned by OpenRouter.")
    except Exception as e:
        raise HTTPException(401, detail=f"OAuth exchange failed: {str(e)}")

    provider = "openrouter"
    _upsert_setting(db, "ai_provider", provider)
    _upsert_setting(db, "ai_base_url", ai_client.PRESETS[provider]["base_url"])
    _upsert_setting(db, "ai_api_key", key)
    if not db.query(SettingsModel).filter(SettingsModel.setting_key == "ai_model").first():
        _upsert_setting(db, "ai_model", ai_client.PRESETS[provider]["models"][0])
    db.commit()
    ai_client.invalidate_config_cache()
    return {"status": "success", "provider": provider}

# -----------------------------------------------------------------------------
# 4.7 Catalog (browseable curated public-domain art; lazy high-res on add)
# -----------------------------------------------------------------------------
# Split manifest produced by tools/build_catalog.py: an index.json (collection summaries) plus one
# <collection_id>.json per collection (the items). Lets the catalog scale to thousands and the UI
# load a collection's thumbnails only when opened.
CATALOG_DIR = Path("static/catalog")
# SD_USER_AGENT (the descriptive UA Wikimedia/museums require) now lives in config.py so the
# offline tools/ scripts can reuse it without importing this app.

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

# _catalog_remote_base, _fetch_remote_json now live in core/settings_util.py (imported above).

# Subscribed (federated) collections share the catalog browse surface, but their ids are namespaced
# so they can never collide with — or masquerade as — a bundled/official collection.
SUB_PREFIX = "sub_"


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

# _download_image_to_library, _focal_xy now live in core/downloads.py (imported above).


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

# ---------------------------------------------------------------------------
# §4.8 Samsung Frame TV push (Integrations)
# ---------------------------------------------------------------------------

async def _frame_select(playlist: str):
    """Selector injected into the Frame pusher: pick the current artwork for a playlist (reusing the
    bag-shuffle/affinity in get_next_image, on a dedicated display_id) and return (file_path, id)."""
    db = SessionLocal()
    try:
        pl = playlist
        if not pl:
            first = db.query(PlaylistModel).order_by(PlaylistModel.id).first()
            if not first:
                return None
            pl = first.name
        cfg = frame_push.get_frame_config()
        info = await select_next_image(
            playlist_name=pl, shuffle=None, display_id=cfg["display_id"], direction=1, db=db
        )
        art_id = (info.get("metadata") or {}).get("id")
        if not art_id:
            return None
        art = db.query(ArtworkModel).filter(ArtworkModel.id == art_id).first()
        if not art:
            return None
        return (LIBRARY_DIR / art.filename, art_id, (art.focal_x, art.focal_y))
    except Exception as e:
        logger.warning(f"[Frame] selection failed: {e}")
        return None
    finally:
        db.close()


@app.get("/api/settings/frame")
async def get_frame_settings(db: Session = Depends(get_db)):
    """Current Frame TV config (+ last-push status) for the Settings panel."""
    cfg = frame_push.get_frame_config(force=True)
    return {
        "enabled": cfg["enabled"],
        "host": cfg["host"],
        "port": cfg["port"],
        "playlist": cfg["playlist"],
        "interval_sec": cfg["interval_sec"],
        "width": cfg["width"],
        "height": cfg["height"],
        "matte": cfg["matte"],
        "last_artwork_id": cfg["last_artwork_id"],
        "last_push_at": cfg["last_push_at"],
    }


class FrameSettingsPayload(BaseModel):
    enabled: bool = False
    host: Optional[str] = ""
    port: Optional[int] = 8001
    playlist: Optional[str] = ""
    interval_sec: Optional[int] = 900
    width: Optional[int] = 3840
    height: Optional[int] = 2160
    matte: Optional[str] = "none"


@app.post("/api/settings/frame")
async def save_frame_settings(payload: FrameSettingsPayload, db: Session = Depends(get_db)):
    """Persist Frame TV settings. Takes effect on the next push cycle (config cache invalidated)."""
    if payload.enabled and not (payload.host or "").strip():
        raise HTTPException(400, "A Frame TV host/IP is required to enable pushing.")
    _upsert_setting(db, "frame_enabled", "true" if payload.enabled else "false")
    _upsert_setting(db, "frame_host", (payload.host or "").strip())
    _upsert_setting(db, "frame_port", str(payload.port or 8001))
    _upsert_setting(db, "frame_playlist", (payload.playlist or "").strip())
    _upsert_setting(db, "frame_interval_sec", str(max(60, payload.interval_sec or 900)))
    _upsert_setting(db, "frame_width", str(payload.width or 3840))
    _upsert_setting(db, "frame_height", str(payload.height or 2160))
    _upsert_setting(db, "frame_matte", (payload.matte or "none").strip())
    db.commit()
    frame_push.invalidate_frame_cache()
    return {"status": "success"}


@app.post("/api/settings/frame/test")
async def test_frame_push(db: Session = Depends(get_db)):
    """One-shot 'Test / Push now'. Returns a structured result (never 500s) so the GUI can show a
    clean message with or without a TV present."""
    return await frame_push.run_test_push(_frame_select)


class CatalogSourcePayload(BaseModel):
    catalog_url: Optional[str] = ""


@app.get("/api/settings/catalog")
async def get_catalog_source(db: Session = Depends(get_db)):
    """Current remote catalog base URL (empty ⇒ serving the bundled catalog)."""
    base = await _catalog_remote_base(db)
    return {"catalog_url": base or "", "using_remote": bool(base)}


@app.post("/api/settings/catalog")
async def save_catalog_source(payload: CatalogSourcePayload, db: Session = Depends(get_db)):
    """Set or clear the remote catalog base URL — a static host serving `index.json` + per-collection
    files (no server required). Validation is advisory: we test-fetch `index.json` and report the
    collection count, but still persist a currently-unreachable URL (the runtime fetch falls back to
    bundled on any failure) so the GUI can warn rather than block. An empty value reverts to bundled."""
    url = (payload.catalog_url or "").strip().rstrip("/")
    if not url:
        row = db.query(SettingsModel).filter(SettingsModel.setting_key == "catalog_url").first()
        if row:
            db.delete(row); db.commit()
        return {"status": "success", "catalog_url": "", "using_remote": False,
                "message": "Reverted to the bundled catalog."}
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Catalog URL must start with http:// or https://")

    warning = None
    collections = 0
    try:
        index = await _fetch_remote_json(url, "index.json")
        if not isinstance(index, dict) or "collections" not in index:
            raise HTTPException(400, "Reached the URL, but it doesn't look like a catalog index "
                                     "(no 'collections' key). Point at the base path that serves "
                                     "index.json.")
        collections = len(index.get("collections") or [])
    except HTTPException:
        raise
    except Exception as e:
        warning = (f"Saved, but couldn't reach {url}/index.json right now ({e}). The app will keep "
                   f"using the bundled catalog until it becomes reachable.")

    _upsert_setting(db, "catalog_url", url)
    db.commit()
    result = {"status": "success", "catalog_url": url, "using_remote": True}
    if warning:
        result["warning"] = warning
    else:
        result["message"] = f"Connected — {collections} collection(s) found."
    return result


@app.get("/api/catalog")
async def get_catalog(db: Session = Depends(get_db)):
    """Collection summaries (cover + count) for the Browse Catalog grid. Items load per-collection."""
    return await _catalog_index(db)

@app.get("/api/catalog/search")
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

@app.get("/api/catalog/suggest")
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

@app.get("/api/catalog/{collection_id}")
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

@app.post("/api/catalog/add")
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

@app.post("/api/catalog/add-bulk")
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

@app.post("/api/catalog/add-collection")
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

# -----------------------------------------------------------------------------
# §4.9 Federation — subscribe to third-party Manifest v2 collections by URL
# -----------------------------------------------------------------------------

def _sub_summary(s: SubscriptionModel) -> dict:
    return {
        "id": s.id,
        "url": s.url,
        "collection_id": f"{SUB_PREFIX}{s.id}",
        "title": s.title,
        "publisher": {"id": s.publisher_id, "name": s.publisher_name, "url": s.publisher_url},
        "trust": s.trust,
        "enabled": s.enabled,
        "item_count": s.item_count,
        "last_synced": s.last_synced.isoformat() if s.last_synced else None,
        "last_status": s.last_status,
    }

class SubscriptionPayload(BaseModel):
    url: str

@app.get("/api/subscriptions")
async def list_subscriptions(db: Session = Depends(get_db)):
    return [_sub_summary(s) for s in db.query(SubscriptionModel).order_by(SubscriptionModel.id).all()]

@app.post("/api/subscriptions")
async def add_subscription(payload: SubscriptionPayload, db: Session = Depends(get_db)):
    """Subscribe to a publisher's Manifest v2 URL. Fetched + safety-checked + validated BEFORE a row
    is created, so a bad/unsafe URL never persists. Trust starts at 'community' (URL-added)."""
    url = payload.url.strip()
    if db.query(SubscriptionModel).filter(SubscriptionModel.url == url).first():
        raise HTTPException(409, detail="Already subscribed to this URL")
    try:
        manifest = await federation.fetch_manifest(url)
    except federation.FederationError as e:
        raise HTTPException(400, detail=str(e)) from e
    pub = manifest.get("publisher") or {}
    sub = SubscriptionModel(
        url=url, collection_id=manifest.get("id"), title=manifest.get("title"),
        publisher_id=pub.get("id"), publisher_name=pub.get("name"), publisher_url=pub.get("url"),
        trust=federation.assess_trust(manifest), enabled=True, cached_manifest=json.dumps(manifest),
        item_count=len(manifest.get("items", [])), last_status="ok", last_synced=datetime.now(UTC))
    db.add(sub); db.commit(); db.refresh(sub)
    return _sub_summary(sub)

@app.post("/api/subscriptions/{sub_id}/sync")
async def sync_subscription_endpoint(sub_id: int, db: Session = Depends(get_db)):
    sub = db.query(SubscriptionModel).filter(SubscriptionModel.id == sub_id).first()
    if not sub:
        raise HTTPException(404)
    await federation.sync_subscription(db, sub)
    return _sub_summary(sub)

@app.delete("/api/subscriptions/{sub_id}")
async def delete_subscription(sub_id: int, db: Session = Depends(get_db)):
    sub = db.query(SubscriptionModel).filter(SubscriptionModel.id == sub_id).first()
    if not sub:
        raise HTTPException(404)
    db.delete(sub); db.commit()
    return {"status": "removed"}

# -----------------------------------------------------------------------------
# 4.9 Publisher Studio — author + sign a Manifest v2 feed of your OWN hosted images.
# The mirror of Subscriptions: that consumes feeds, this AUTHORS one. Images are URL-first
# (we never host them); the artist's Ed25519 identity key lives in SettingsModel and signs
# server-side on export; the browser never sees the private key.
# -----------------------------------------------------------------------------
_PUB_IDENTITY_KEYS = ("publisher_id", "publisher_name", "publisher_url",
                      "publisher_public_key", "publisher_private_key")


def _publisher_identity(db: Session) -> dict:
    rows = {s.setting_key: s.setting_value for s in
            db.query(SettingsModel).filter(SettingsModel.setting_key.in_(_PUB_IDENTITY_KEYS)).all()}
    return rows


def _identity_public(rows: dict) -> dict:
    """Public-safe view of the identity — NEVER includes the private key."""
    return {
        "id": rows.get("publisher_id") or "",
        "name": rows.get("publisher_name") or "",
        "url": rows.get("publisher_url") or "",
        "public_key": rows.get("publisher_public_key") or "",
        "has_private_key": bool(rows.get("publisher_private_key")),
    }


async def _assert_public_urls(items: list) -> None:
    """SSRF-guard every image URL the publisher pasted (defense in depth: the subscriber checks too,
    but we never persist a private/loopback target). C2: getaddrinfo is blocking → thread each check."""
    for it in items or []:
        for url in (it.get("full_url"), it.get("thumbnail_url")):
            if not url:
                continue
            try:
                await asyncio.to_thread(federation._assert_public_url, url)
            except federation.FederationError as e:
                raise HTTPException(400, detail=f"Image URL rejected ({url}): {e}") from e


def _collection_summary(c: PublisherCollectionModel) -> dict:
    try:
        items = json.loads(c.items_json or "[]")
    except ValueError:
        items = []
    return {"id": c.id, "slug": c.slug, "title": c.title, "item_count": len(items),
            "updated_at": c.updated_at.isoformat() if c.updated_at else None}


def _collection_detail(c: PublisherCollectionModel) -> dict:
    try:
        items = json.loads(c.items_json or "[]")
    except ValueError:
        items = []
    return {"id": c.id, "slug": c.slug, "title": c.title, "description": c.description,
            "default_license": c.default_license, "cover_image": c.cover_image, "items": items,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None}


def _unique_slug(db: Session, desired: str, exclude_id: int | None = None) -> str:
    base = publisher._slugify(desired)
    slug, n = base, 2
    while True:
        q = db.query(PublisherCollectionModel).filter(PublisherCollectionModel.slug == slug)
        if exclude_id is not None:
            q = q.filter(PublisherCollectionModel.id != exclude_id)
        if not q.first():
            return slug
        slug, n = f"{base}-{n}", n + 1


def _meta_for(c: PublisherCollectionModel, identity: dict) -> dict:
    return {"slug": c.slug, "title": c.title, "description": c.description,
            "default_license": c.default_license, "cover_image": c.cover_image,
            "publisher": {"id": identity.get("publisher_id"), "name": identity.get("publisher_name"),
                          "url": identity.get("publisher_url")}}


class PublisherIdentityPayload(BaseModel):
    id: str
    name: str
    url: Optional[str] = None
    regenerate: bool = False


class PublisherItemPayload(BaseModel):
    id: Optional[str] = None
    title: str
    artist: Optional[str] = None
    artist_role: Optional[str] = None
    date: Optional[str] = None
    creation_date: Optional[str] = None
    medium: Optional[str] = None
    culture: Optional[str] = None
    tags: Optional[List[str]] = None
    placard: Optional[str] = None
    full_url: str
    thumbnail_url: Optional[str] = None
    license: Optional[str] = None
    attribution: Optional[str] = None
    rights_holder: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    focal_point: Optional[List[float]] = None


class PublisherCollectionPayload(BaseModel):
    slug: Optional[str] = None
    title: str
    description: Optional[str] = None
    default_license: Optional[str] = None
    cover_image: Optional[str] = None
    items: List[PublisherItemPayload] = []


@app.get("/api/publisher/identity")
async def get_publisher_identity(db: Session = Depends(get_db)):
    return _identity_public(_publisher_identity(db))


@app.post("/api/publisher/identity")
async def set_publisher_identity(payload: PublisherIdentityPayload, db: Session = Depends(get_db)):
    """Save the publisher id/name/url and ensure an Ed25519 identity key exists. Generates a keypair on
    first save; `regenerate=true` rotates it — which invalidates the signature on anything already
    published (the response carries a warning the UI surfaces)."""
    pid = payload.id.strip()
    name = payload.name.strip()
    if not pid or not name:
        raise HTTPException(400, detail="Publisher id and name are required.")
    rows = _publisher_identity(db)
    _upsert_setting(db, "publisher_id", pid)
    _upsert_setting(db, "publisher_name", name)
    _upsert_setting(db, "publisher_url", (payload.url or "").strip())
    warning = None
    if payload.regenerate or not rows.get("publisher_private_key"):
        priv, pub = publisher.keygen()
        _upsert_setting(db, "publisher_private_key", priv)
        _upsert_setting(db, "publisher_public_key", pub)
        if payload.regenerate and rows.get("publisher_private_key"):
            warning = ("Signing key rotated. Any manifest you already published is now signed with the "
                       "old key — re-export and re-host it, and update the registry if you were verified.")
    db.commit()
    result = _identity_public(_publisher_identity(db))
    if warning:
        result["warning"] = warning
    return result


@app.get("/api/publisher/collections")
async def list_publisher_collections(db: Session = Depends(get_db)):
    return [_collection_summary(c) for c in
            db.query(PublisherCollectionModel).order_by(PublisherCollectionModel.id).all()]


async def _checked_cover(payload: PublisherCollectionPayload) -> str | None:
    cover = (payload.cover_image or "").strip() or None
    if cover:
        await _assert_public_urls([{"full_url": cover}])
    return cover


@app.post("/api/publisher/collections")
async def create_publisher_collection(payload: PublisherCollectionPayload, db: Session = Depends(get_db)):
    items = [it.model_dump() for it in payload.items]
    await _assert_public_urls(items)
    cover = await _checked_cover(payload)
    slug = _unique_slug(db, payload.slug or payload.title)
    norm = [publisher.build_item(it) for it in items]
    c = PublisherCollectionModel(
        slug=slug, title=payload.title.strip(), description=(payload.description or "").strip() or None,
        default_license=(payload.default_license or "").strip() or None, cover_image=cover,
        items_json=json.dumps(norm), created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    db.add(c); db.commit(); db.refresh(c)
    return _collection_detail(c)


def _get_collection(db: Session, cid: int) -> PublisherCollectionModel:
    c = db.query(PublisherCollectionModel).filter(PublisherCollectionModel.id == cid).first()
    if not c:
        raise HTTPException(404, detail="Collection not found")
    return c


@app.get("/api/publisher/collections/{cid}")
async def get_publisher_collection(cid: int, db: Session = Depends(get_db)):
    return _collection_detail(_get_collection(db, cid))


@app.put("/api/publisher/collections/{cid}")
async def update_publisher_collection(cid: int, payload: PublisherCollectionPayload,
                                      db: Session = Depends(get_db)):
    c = _get_collection(db, cid)
    items = [it.model_dump() for it in payload.items]
    await _assert_public_urls(items)
    cover = await _checked_cover(payload)
    if payload.slug and publisher._slugify(payload.slug) != c.slug:
        c.slug = _unique_slug(db, payload.slug, exclude_id=c.id)
    c.title = payload.title.strip()
    c.description = (payload.description or "").strip() or None
    c.default_license = (payload.default_license or "").strip() or None
    c.cover_image = cover
    c.items_json = json.dumps([publisher.build_item(it) for it in items])
    c.updated_at = datetime.now(UTC)
    db.commit(); db.refresh(c)
    return _collection_detail(c)


@app.delete("/api/publisher/collections/{cid}")
async def delete_publisher_collection(cid: int, db: Session = Depends(get_db)):
    db.delete(_get_collection(db, cid)); db.commit()
    return {"status": "removed"}


@app.post("/api/publisher/collections/{cid}/validate")
async def validate_publisher_collection(cid: int, db: Session = Depends(get_db)):
    c = _get_collection(db, cid)
    items = json.loads(c.items_json or "[]")
    _, errors = publisher.assemble_and_validate(_meta_for(c, _publisher_identity(db)), items)
    return {"valid": not errors, "errors": errors}


@app.post("/api/publisher/collections/{cid}/export")
async def export_publisher_collection(cid: int, db: Session = Depends(get_db)):
    """Assemble → validate → sign → download. 400 if no identity/key; 422 if the manifest is invalid."""
    c = _get_collection(db, cid)
    identity = _publisher_identity(db)
    if not identity.get("publisher_private_key"):
        raise HTTPException(400, detail="Set up your publisher identity first (it creates a signing key).")
    items = json.loads(c.items_json or "[]")
    await _assert_public_urls(items)
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    manifest, errors = publisher.assemble_validate_sign(
        _meta_for(c, identity), items,
        identity["publisher_private_key"], identity.get("publisher_public_key"),
        generated_at=generated_at)
    if errors:
        raise HTTPException(422, detail=errors)
    return Response(
        content=json.dumps(manifest, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{c.slug}.json"'})


# -----------------------------------------------------------------------------
# 5. Static File Serving
# -----------------------------------------------------------------------------
if ARTWORK_ROOT.exists():
    app.mount("/media", StaticFiles(directory=str(ARTWORK_ROOT)), name="media")
STATIC_DIR = Path("static")
@app.get("/admin")
async def get_admin_page(): return FileResponse(STATIC_DIR / "admin.html")

@app.get("/help")
async def get_help_page(): return FileResponse(STATIC_DIR / "help.html")

@app.get("/studio")
async def get_studio_page(): return FileResponse(STATIC_DIR / "studio.html")

@app.get("/publisher")
async def get_publisher_page():
    # Publisher Studio is now a view inside the admin SPA; keep this path working for bookmarks/links.
    return RedirectResponse(url="/admin?view=publisher")

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
