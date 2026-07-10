"""FastAPI lifespan (boot) machinery — leader election, migrations, filesystem sync, factory seed,
and the Canvas cache warmer. Extracted verbatim from app.py (Phase 4 of the app-split refactor).
"""

import asyncio
import fcntl
import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

import frame_push
from config import ARTWORK_ROOT, LIBRARY_DIR
from core.downloads import _download_image_to_library, _focal_xy
from core.media import render_canvas_image
from core.playback import _frame_select
from database import SessionLocal
from db_migrate import run_migrations
from models import ArtworkModel, PlaylistModel, playlist_artwork

logger = logging.getLogger("artwork-display-api")


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
