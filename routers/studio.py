"""Studio → My Photos: personal-photo upload, captioning, editing, and albums — extracted from
app.py (Phase 3 of the app-split refactor).

Unlike the museum upload path (routers/library.py), personal photos never go through the AI
enrichment pipeline or the review queue — a photo is never sent to a model unless the user
explicitly asks for a caption suggestion, and it lands `approved` immediately (the privacy headline).
"""

import asyncio
import io
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageOps
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

import ai_client
from config import LIBRARY_DIR
from core.media import warm_canvas_cache_async
from core.playlists import _link_artwork_to_playlist
from core.schemas import ArtworkSchema
from database import get_db
from models import ArtworkModel, PlaylistModel, playlist_artwork

logger = logging.getLogger("artwork-display-api")

router = APIRouter()


PERSONAL_PLAYLIST_NAME = "My Photos"


def _get_or_create_playlist(db: Session, name: str, is_personal: bool = False) -> PlaylistModel:
    pl = db.query(PlaylistModel).filter(PlaylistModel.name == name).first()
    if not pl:
        pl = PlaylistModel(name=name, is_personal=is_personal)
        db.add(pl); db.commit(); db.refresh(pl)
    elif is_personal and not pl.is_personal:
        pl.is_personal = True; db.commit()   # self-heal a pre-existing "My Photos" created before the flag
    return pl



@router.post("/upload/personal", response_model=ArtworkSchema)
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


@router.post("/api/studio/caption/{artwork_id}")
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


@router.patch("/api/studio/photo/{artwork_id}", response_model=ArtworkSchema)
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


@router.get("/api/studio/photos")
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


@router.get("/api/studio/albums")
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


@router.post("/api/studio/albums")
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


@router.delete("/api/studio/albums/{album_id}")
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
