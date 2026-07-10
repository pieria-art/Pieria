#!/usr/bin/env python3
"""
FastAPI Backend for the Artwork Display Engine.
Phase 4: Targeted WebSocket Routing for Multiple Displays.
"""

import asyncio
import fcntl
import io
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

import pillow_heif
from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
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

import ai_client

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
    warm_canvas_cache_async,
)
from core.playback import (  # noqa: E402
    _display_now_playing,
    _frame_select,
    _playlist_name_if_playable,
    select_next_image,
    touch_active_display,
)
from core.playlists import _link_artwork_to_playlist  # noqa: E402
from core.schemas import ArtworkSchema  # noqa: E402

# Origin/CORS trust checks used by the middleware below (see core/security.py).
from core.security import (  # noqa: E402
    _PUBLIC_FEED_GET_PREFIXES,
    _origin_allowed,
    _same_origin,  # noqa: F401  — re-exported; used only by the middleware below
)

# Settings-table read/write + schedule helpers (see core/settings_util.py).
from core.settings_util import (  # noqa: E402
    _HHMM_RE,
    DEFAULT_SCHEDULE,  # noqa: F401  — re-exported for tests/test_schedule.py
    _load_schedule,
    _parse_hhmm,
    resolve_schedule_state,  # noqa: F401  — re-exported for tests/test_schedule.py
)
from database import SessionLocal, get_db
from epaper import PALETTES, VALID_FORMATS, media_type_for, render_for_epaper
from models import (
    ActiveDisplayModel,
    ArtworkModel,
    DiscoveryQueueModel,
    PlaylistModel,
    RemoteCommandModel,
    SettingsModel,
    playlist_artwork,
)

# Leaf domain routers extracted from app.py (Phase 1 + Phase 2 of the app-split refactor). Each is a
# plain APIRouter with no dependency on this module — see routers/__init__.py for the import rule.
from routers.catalog import _read_local_json  # noqa: F401  — re-exported for tests/test_cache.py
from routers.catalog import router as catalog_router
from routers.curation import router as curation_router
from routers.federation import router as federation_router
from routers.health import router as health_router
from routers.library import router as library_router
from routers.pages import router as pages_router
from routers.publisher import router as publisher_router
from routers.settings import router as settings_router


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

# Leaf domain routers (Phase 1 + Phase 2 of the app-split refactor — see .ai/refactor_app_split_plan.md).
app.include_router(publisher_router)
app.include_router(federation_router)
app.include_router(health_router)
app.include_router(pages_router)
app.include_router(settings_router)
app.include_router(library_router)
app.include_router(curation_router)
app.include_router(catalog_router)

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

class RemoteChangeRequest(BaseModel):
    target_display: str
    action: str
    playlist: Optional[str] = None
    mode: Optional[str] = None


# -----------------------------------------------------------------------------
# 3. API Endpoints
# -----------------------------------------------------------------------------


PERSONAL_PLAYLIST_NAME = "My Photos"


def _get_or_create_playlist(db: Session, name: str, is_personal: bool = False) -> PlaylistModel:
    pl = db.query(PlaylistModel).filter(PlaylistModel.name == name).first()
    if not pl:
        pl = PlaylistModel(name=name, is_personal=is_personal)
        db.add(pl); db.commit(); db.refresh(pl)
    elif is_personal and not pl.is_personal:
        pl.is_personal = True; db.commit()   # self-heal a pre-existing "My Photos" created before the flag
    return pl



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

# --- R1-F2: Night & Quiet Hours (clock-driven brightness/warmth + quiet-hours panel power) ----------
# The settings routes (GET/POST /api/settings/display-schedule) now live in routers/settings.py; the
# resolver (resolve_schedule_state), its minute-math helpers (_parse_hhmm/_cyc_*), DEFAULT_SCHEDULE,
# SCHEDULE_SETTING_KEY, and _HHMM_RE all live in core/settings_util.py (imported above) — this
# schedule-state route is display-domain (not settings) and is the one caller left here.


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
# GET /remote (the page) now lives in routers/pages.py.
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

# /api/health/host + the appliance update bridge (/api/appliance/update*) now live in
# routers/health.py.

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
# 4.5 Settings (API Keys) — GET/POST /api/settings/keys* now live in routers/settings.py.
# -----------------------------------------------------------------------------

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

