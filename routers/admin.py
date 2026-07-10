"""Admin-only destructive endpoints. Extracted verbatim from app.py (Phase 4 of the app-split
refactor)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.orm import Session

from config import ARTWORK_ROOT, LIBRARY_DIR
from core.media import DERIVATIVES_DIR
from database import get_db
from models import ArtworkModel, DiscoveryQueueModel, playlist_artwork

logger = logging.getLogger("artwork-display-api")

router = APIRouter()


class FactoryResetRequest(BaseModel):
    confirm: str = ""


@router.post("/api/admin/factory-reset")
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
