"""Library, playlists, artwork lifecycle, and admin image serving — extracted from app.py
(Phase 2 of the app-split refactor). Upload triggers the AI pipeline via `run_ai_pipeline`
(background task); `_wipe_artwork` is shared by the single and bulk delete routes.
"""

import html
import io
import json
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from PIL import Image, ImageOps
from pydantic import BaseModel
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

import federation
from agents import process_artwork
from config import LIBRARY_DIR, strip_markdown
from core.media import get_optimized_image
from core.playback import placard_metadata
from core.playlists import _link_artwork_to_playlist
from core.schemas import ArtworkSchema
from database import SessionLocal, get_db
from models import ArtworkModel, PlaylistModel, SubscriptionModel, playlist_artwork

logger = logging.getLogger("artwork-display-api")

router = APIRouter()


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
    # The Collection (pack/sub) this Gallery was minted from, resolved to its title by list_playlists;
    # None for a user-built gallery. Drives the "from your <name> Collection" source line + cross-link.
    source_collection: Optional[str] = None
    # True once this Gallery diverges from its Collection (a Collection work removed, or an outside work
    # added) — the UI shows a "· edited" tag. `collection_missing` is how many of the Collection's works
    # were removed (still in the library) — the UI offers Restore only when this is > 0.
    collection_modified: bool = False
    collection_missing: int = 0
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


async def run_ai_pipeline(artwork_id: int):
    db = SessionLocal()
    try:
        await process_artwork(artwork_id, db)
    finally:
        db.close()


@router.get("/artworks", response_model=List[ArtworkSchema])
async def get_full_library(db: Session = Depends(get_db)):
    return db.query(ArtworkModel).all()

@router.get("/playlists", response_model=List[PlaylistSchema])
async def list_playlists(db: Session = Depends(get_db)):
    # Underscore-prefixed names are internal pseudo-collections (e.g. "_derivatives", the optimized-image
    # display cache) — never real collections. Keep their rows + cached images, but never surface them in
    # the UI. Mirrors the sync-time skip of "_"-prefixed dirs; this is the matching display-layer guard,
    # so even a stale "_" playlist (created before that skip existed) stays hidden everywhere /playlists
    # feeds: the admin sidebar, the "Add to" picker, and the Canvas first-non-empty fallback.
    playlists = [p for p in db.query(PlaylistModel).all() if not p.name.startswith("_")]
    # For each auto-minted Gallery: resolve its Collection title (source line) + whether it has diverged
    # from that Collection (the "· edited" tag). Parse each linked Collection's manifest once.
    linked_ids = {p.source_subscription_id for p in playlists if p.source_subscription_id}
    subs, manifests = {}, {}  # sub_id -> SubscriptionModel ; sub_id -> set(source_urls)
    if linked_ids:
        for s in db.query(SubscriptionModel).filter(SubscriptionModel.id.in_(linked_ids)).all():
            subs[s.id] = s
            urls = set()
            if s.cached_manifest:
                try:
                    m = json.loads(s.cached_manifest)
                    urls = {(federation.manifest_item_to_catalog(it) or {}).get("source_url")
                            for it in m.get("items", [])}
                    urls.discard(None)
                except (ValueError, TypeError):
                    urls = set()
            manifests[s.id] = urls
    # Which of those Collection works actually exist in the library (installed) — one query.
    all_urls = set().union(*manifests.values()) if manifests else set()
    installed_urls = ({u for (u,) in db.query(ArtworkModel.source_url).filter(
        ArtworkModel.source_url.in_(all_urls)).all() if u} if all_urls else set())
    for p in playlists:
        s = subs.get(p.source_subscription_id)
        p.source_collection = (s.title or s.collection_id or "") if s else None
        p.collection_modified = False
        p.collection_missing = 0
        if s is not None:
            coll = manifests.get(s.id, set())
            gallery_urls = {a.source_url for a in p.artworks if a.source_url}
            missing = (coll & installed_urls) - gallery_urls  # Collection works removed (still restorable)
            added = bool(gallery_urls - coll)                 # a work not from the Collection
            p.collection_missing = len(missing)
            p.collection_modified = bool(missing) or added
    return playlists

@router.post("/playlists", response_model=PlaylistSchema)
async def create_playlist(name: str = Form(...), db: Session = Depends(get_db)):
    existing = db.query(PlaylistModel).filter(PlaylistModel.name == name).first()
    if existing: raise HTTPException(status_code=400, detail="Exists")
    new_p = PlaylistModel(name=name); db.add(new_p); db.commit(); db.refresh(new_p)
    return new_p

@router.patch("/playlists/{playlist_id}", response_model=PlaylistSchema)
async def update_playlist(playlist_id: int, data: PlaylistUpdate, db: Session = Depends(get_db)):
    p = db.query(PlaylistModel).filter(PlaylistModel.id == playlist_id).first()
    if not p: raise HTTPException(404)
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

@router.delete("/playlists/{playlist_id}")
async def delete_playlist(playlist_id: int, db: Session = Depends(get_db)):
    p = db.query(PlaylistModel).filter(PlaylistModel.id == playlist_id).first()
    if not p: raise HTTPException(404)
    db.delete(p); db.commit(); return {"status": "ok"}

@router.post("/playlists/{playlist_id}/restore-from-collection")
async def restore_gallery(playlist_id: int, db: Session = Depends(get_db)):
    """Re-add the source Collection's works that were removed from this Gallery (non-destructive). 400 if
    the gallery isn't Collection-linked. Works deleted from the library need a Collection re-download."""
    from core import lifespan as lifespan_module  # lazy import — avoids import-time coupling
    res = lifespan_module.restore_gallery_from_collection(db, playlist_id)
    if res is None:
        raise HTTPException(400, detail="This gallery isn't linked to a Collection.")
    return res

@router.post("/playlists/{playlist_id}/artworks/{artwork_id}")
async def link_artwork_to_playlist(playlist_id: int, artwork_id: int, db: Session = Depends(get_db)):
    db.execute(playlist_artwork.insert().values(playlist_id=playlist_id, artwork_id=artwork_id))
    db.commit(); return {"status": "linked"}

@router.delete("/playlists/{playlist_id}/artworks/{artwork_id}")
async def unlink_artwork_from_playlist(playlist_id: int, artwork_id: int, db: Session = Depends(get_db)):
    db.execute(delete(playlist_artwork).where(
        playlist_artwork.c.playlist_id == playlist_id,
        playlist_artwork.c.artwork_id == artwork_id
    ))
    db.commit(); return {"status": "unlinked"}

@router.post("/playlists/{playlist_id}/artworks")
async def link_artworks_to_playlist(playlist_id: int, payload: ArtworkIds, db: Session = Depends(get_db)):
    """Bulk add: link many library artworks to a playlist in one call (the multi-select 'Add from
    Library'). Idempotent per artwork — reuses _link_artwork_to_playlist, which skips existing links
    and appends in order. A distinct path from the single /{artwork_id} POST, so no route collision."""
    for aid in payload.artwork_ids:
        _link_artwork_to_playlist(db, playlist_id, aid)
    return {"status": "linked", "count": len(payload.artwork_ids)}

@router.delete("/playlists/{playlist_id}/artworks")
async def unlink_artworks_from_playlist(playlist_id: int, payload: ArtworkIds, db: Session = Depends(get_db)):
    """Bulk remove: unlink many artworks from a playlist (multi-select Remove). Removes only the
    association — the artworks stay in the library."""
    n = db.execute(delete(playlist_artwork).where(
        playlist_artwork.c.playlist_id == playlist_id,
        playlist_artwork.c.artwork_id.in_(payload.artwork_ids or [-1]))).rowcount
    db.commit(); return {"status": "unlinked", "count": n}

@router.post("/playlists/{playlist_id}/reorder")
async def reorder_playlist(playlist_id: int, request: ReorderRequest, db: Session = Depends(get_db)):
    for index, art_id in enumerate(request.artwork_ids):
        db.execute(update(playlist_artwork).where(
            playlist_artwork.c.playlist_id == playlist_id,
            playlist_artwork.c.artwork_id == art_id
        ).values(display_order=index))
    db.commit(); return {"status": "success"}

@router.post("/upload", response_model=ArtworkSchema)
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

@router.get("/artworks/pending", response_model=List[ArtworkSchema])
async def get_pending_artworks(db: Session = Depends(get_db)):
    return db.query(ArtworkModel).filter(ArtworkModel.status == 'pending_review').all()

@router.patch("/artworks/{artwork_id}/approve", response_model=ArtworkSchema)
async def approve_artwork(artwork_id: int, data: ArtworkApproval, db: Session = Depends(get_db)):
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    art.title, art.agent_name, art.agent_role, art.creation_date, art.cultural_context, art.medium, art.date_display, art.description_narrative, art.tags, art.status = data.title, data.agent_name, data.agent_role, data.creation_date, data.cultural_context, data.medium, data.date_display, data.description_narrative, data.tags, 'approved'
    db.commit(); db.refresh(art); return art

@router.post("/artworks/approve-bulk")
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

@router.patch("/artworks/{artwork_id}/metadata", response_model=ArtworkSchema)
async def update_artwork_metadata(artwork_id: int, data: ArtworkApproval, db: Session = Depends(get_db)):
    """Edit an already-approved artwork's placard metadata in place — the Edit landing's Save for
    museum/catalog works. Unlike /approve (the Review-Queue publish step), this does NOT touch status,
    so an approved piece stays approved. Personal photos edit via /api/studio/photo instead."""
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    art.title, art.agent_name, art.agent_role, art.creation_date, art.cultural_context, art.medium, art.date_display, art.description_narrative, art.tags = data.title, data.agent_name, data.agent_role, data.creation_date, data.cultural_context, data.medium, data.date_display, data.description_narrative, data.tags
    db.commit(); db.refresh(art); return art

@router.get("/artworks/{artwork_id}/thumbnail")
async def get_artwork_thumbnail(artwork_id: int, db: Session = Depends(get_db)):
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    path = LIBRARY_DIR / art.filename
    # A1: Pillow decode/resize/encode is blocking — thread it so a cold admin grid (dozens of concurrent
    # misses) doesn't serialize on the worker's event loop. Mirrors /display.jpg below.
    data = await run_in_threadpool(get_optimized_image, path, (400, 400), quality=70)
    return Response(content=data, media_type="image/jpeg")

@router.get("/artworks/{artwork_id}/preview")
async def get_artwork_preview(artwork_id: int, db: Session = Depends(get_db)):
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    path = LIBRARY_DIR / art.filename
    data = await run_in_threadpool(get_optimized_image, path, (1920, 1080), quality=85)
    return Response(content=data, media_type="image/jpeg")

@router.get("/art/{artwork_id}", response_class=HTMLResponse)
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
<title>{title} — Pieria</title><style>
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
 <div class=brand><img src="/logo.svg" alt=""> Presented by Pieria</div>
</div></body></html>""")

@router.get("/artworks/{artwork_id}/placard")
async def get_artwork_placard(artwork_id: int, db: Session = Depends(get_db)):
    """The placard text for one artwork, as JSON — what the phone Remote's 'Read placard' tile renders.

    Exists because an e-ink panel shows art ONLY (render_for_epaper bakes no text), so the phone is the
    placard surface for it. Same key set as /next-image's `metadata` block — both come from
    core.playback.placard_metadata, and tests/test_placard_api.py asserts the two can't drift.

    Markdown is stripped HERE, on exactly the three fields the Canvas runs through stripMd()
    (static/app.js updatePlacard: title, series, description), so remote.html needs no fourth copy of
    that helper. Everything else is passed through raw.

    Reads only — it never triggers enrichment. Placards are generated on upload (agents.process_artwork),
    on admin re-enrich (routers/curation.py), or offline at pack-build time; a null description here just
    means one was never made. Also deliberately NOT gated on status=='approved': /art/{id} and
    /artworks/{id}/preview already serve unapproved rows, so a gate here would be a novel inconsistency
    that exposes nothing new.
    """
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    data = placard_metadata(art)
    for field in ("title", "series", "description"):
        if data[field]:
            data[field] = strip_markdown(data[field])
    # is_personal is passed through, NOT blanked server-side: /next-image doesn't blank either, and the
    # client branches on the flag (app.js and remote.html both). Identical key sets is the whole point.
    return data

class CropPayload(BaseModel):
    crop_x: float = 0.0
    crop_y: float = 0.0
    crop_width: float = 0.0
    crop_height: float = 0.0
    focal_x: Optional[float] = None
    focal_y: Optional[float] = None

@router.patch("/artworks/{artwork_id}/crop", response_model=ArtworkSchema)
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

@router.delete("/artworks/{artwork_id}")
async def permanent_delete_artwork(artwork_id: int, db: Session = Depends(get_db)):
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)
    _wipe_artwork(db, art); db.commit(); return {"status": "wiped"}

@router.post("/artworks/delete")
async def bulk_delete_artworks(payload: ArtworkIds, db: Session = Depends(get_db)):
    """Bulk permanent delete (multi-select Delete in the Library). POST (not DELETE) so the id list
    rides in the body without colliding with DELETE /artworks/{id}. Skips ids that no longer exist."""
    arts = db.query(ArtworkModel).filter(ArtworkModel.id.in_(payload.artwork_ids or [-1])).all()
    for art in arts:
        _wipe_artwork(db, art)
    db.commit(); return {"status": "wiped", "count": len(arts)}
