#!/usr/bin/env python3
"""
FastAPI Backend for the Artwork Display Engine.
Phase 4: Targeted WebSocket Routing for Multiple Displays.
"""

import asyncio
import fcntl
import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import pillow_heif
from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import delete, select
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
# httpx: no longer called directly here (its call sites moved to routers/settings.py + core/downloads.py),
# but tests/test_download.py + tests/test_catalog.py patch `app_module.httpx.AsyncClient` — since it's
# the same singleton module object core.downloads imports, that patch only works while `app` still
# binds the name `httpx` at module scope. Keep the import.
import httpx  # noqa: F401

# federation: no longer called directly here (its call sites moved to routers/catalog.py +
# routers/publisher.py + routers/federation.py), but tests/test_federation.py + tests/test_publisher_api.py
# patch `app_module.federation.*` — since it's the same singleton module object those routers import,
# that patch only works while `app` still binds the name `federation` at module scope. Keep the import.
import federation  # noqa: F401
import frame_push
from config import ARTWORK_ROOT, LIBRARY_DIR, STATIC_DIR

# Targeted WebSocket connection registry (shared by the ws + remote push paths).
from core.connections import ConnectionManager, manager  # noqa: F401

# SSRF-safe downloader + focal-point parsing (see core/downloads.py).
from core.downloads import _download_image_to_library, _focal_xy  # noqa: E402

# Derivative-image rendering primitives (see core/media.py).
from core.media import (  # noqa: E402
    DERIVATIVES_DIR,
    DISPLAY_MAX_EDGE,  # noqa: F401  — re-exported for tests/test_display_image.py
    render_canvas_image,
)
from core.playback import _frame_select  # noqa: E402

# Origin/CORS trust checks used by the middleware below (see core/security.py).
from core.security import (  # noqa: E402
    _PUBLIC_FEED_GET_PREFIXES,
    _origin_allowed,
    _same_origin,  # noqa: F401  — re-exported; used only by the middleware below
)

# Settings-table read/write + schedule helpers (see core/settings_util.py). The resolver + its
# minute-math helpers are only called from routers/display.py now; kept here (unused internally)
# because tests/test_schedule.py imports both directly off `app`.
from core.settings_util import (  # noqa: E402
    DEFAULT_SCHEDULE,  # noqa: F401  — re-exported for tests/test_schedule.py
    resolve_schedule_state,  # noqa: F401  — re-exported for tests/test_schedule.py
)
from database import SessionLocal, get_db
from models import (
    ArtworkModel,
    DiscoveryQueueModel,
    PlaylistModel,
    playlist_artwork,
)

# Leaf domain routers extracted from app.py (Phase 1 + Phase 2 + Phase 3 of the app-split refactor).
# Each is a plain APIRouter with no dependency on this module — see routers/__init__.py for the
# import rule.
from routers.catalog import _read_local_json  # noqa: F401  — re-exported for tests/test_cache.py
from routers.catalog import router as catalog_router
from routers.curation import router as curation_router
from routers.display import router as display_router
from routers.federation import router as federation_router
from routers.health import router as health_router
from routers.library import router as library_router
from routers.pages import router as pages_router
from routers.publisher import router as publisher_router
from routers.settings import router as settings_router
from routers.studio import PERSONAL_PLAYLIST_NAME  # noqa: F401  — re-exported for tests/test_personal.py etc.
from routers.studio import router as studio_router
from routers.ws import router as ws_router


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

# Leaf domain routers (Phase 1 + Phase 2 + Phase 3 of the app-split refactor — see
# .ai/refactor_app_split_plan.md).
app.include_router(publisher_router)
app.include_router(federation_router)
app.include_router(health_router)
app.include_router(pages_router)
app.include_router(settings_router)
app.include_router(library_router)
app.include_router(curation_router)
app.include_router(catalog_router)
app.include_router(studio_router)
app.include_router(display_router)
app.include_router(ws_router)

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

# GET /next-image, /display/{display_id}/current.{ext}, /artworks/{artwork_id}/display.jpg,
# /api/telemetry/heartbeat, /api/displays/{display_id}/preferred-playlist, and
# /api/displays/{display_id}/schedule-state now live in routers/display.py.
#
# GET /api/remote/displays, GET /api/displays/{display_id}/now-playing, POST /api/remote/change,
# and WEBSOCKET /ws/{display_id} now live in routers/ws.py.


# -----------------------------------------------------------------------------
# 4.5 Settings (API Keys) — GET/POST /api/settings/keys* now live in routers/settings.py.
# -----------------------------------------------------------------------------


# POST /api/settings/keys/{source} now lives in routers/settings.py.

# -----------------------------------------------------------------------------
# 4.6 AI Engine (model provider configuration) — /api/settings/ai*, /api/settings/ai/oauth/* now
# live in routers/settings.py.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# 4.7 Catalog (browseable curated public-domain art; lazy high-res on add)
# -----------------------------------------------------------------------------
# CATALOG_DIR, _read_local_json, _subscribed_summaries, _subscribed_collection, _catalog_index,
# _catalog_collection, _download_and_create_artwork, and every /api/catalog* route now live in
# routers/catalog.py (imported above; _read_local_json is re-exported for tests/test_cache.py).

# ---------------------------------------------------------------------------
# §4.8 Samsung Frame TV push (Integrations)
# ---------------------------------------------------------------------------
# _frame_select (the selector shared with lifespan's frame_push_loop AND routers/settings.py's
# "Test / Push now" route) now lives in core/playback.py (imported above) — see that module's
# docstring for why. The /api/settings/frame*, /api/settings/catalog* routes now live in
# routers/settings.py; the /api/catalog* browse + add routes now live in routers/catalog.py.
# Federation (/api/subscriptions*) now lives in routers/federation.py.
# Publisher Studio (/api/publisher/*) now lives in routers/publisher.py.

# -----------------------------------------------------------------------------
# 5. Static File Serving
# -----------------------------------------------------------------------------
if ARTWORK_ROOT.exists():
    app.mount("/media", StaticFiles(directory=str(ARTWORK_ROOT)), name="media")
# The /admin, /help, /studio, /remote page routes + the /publisher redirect now live in
# routers/pages.py.

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)

