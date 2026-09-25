"""The display feed — extracted from app.py (Phase 3 of the app-split refactor).

The Canvas (capped display.jpg + /next-image selection), the e-ink/BYOS pull-on-wake endpoint
(/display/{id}/current.{ext}), telemetry ingestion, and the display-scoped preferred-playlist /
schedule-state resolvers all live here. Selection itself stays in core.playback (select_next_image)
so both the Canvas and e-ink paths advance the same bag-shuffle state.
"""

import hashlib
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

import config
from config import LIBRARY_DIR
from core.demo import normalize_display_id
from core.media import lookup_artwork_filename, peek_canvas_image, render_canvas_image, run_image_work
from core.playback import _playlist_name_if_playable, select_next_image, touch_active_display
from core.settings_util import _HHMM_RE, _load_schedule, _parse_hhmm, resolve_schedule_state
from database import SessionLocal, get_db
from epaper import (
    PALETTES,
    VALID_FORMATS,
    media_type_for,
    pick_crop_for_aspect,
    render_for_epaper,
)
from models import ArtworkModel, PlaylistModel, SettingsModel

logger = logging.getLogger("artwork-display-api")

router = APIRouter()


@router.get("/artworks/{artwork_id}/display.jpg")
async def get_artwork_display(artwork_id: int):
    """Resolution-capped image the Canvas loads instead of the full-res original
    (see render_canvas_image). Ends in .jpg → inherits the immutable media cache tier;
    the ?v=<mtime> query the caller appends busts that cache when the source changes.

    M6 (INFRA-D075 incident): no Depends(get_db) — see routers/library.py's thumbnail endpoint for
    why a session must never be held across the Pillow render."""
    filename = await run_in_threadpool(lookup_artwork_filename, artwork_id)
    path = LIBRARY_DIR / filename
    if not path.exists(): raise HTTPException(404)
    data = await run_image_work(render_canvas_image, path, artwork_id,
                                 cache_check=lambda: peek_canvas_image(path, artwork_id))
    return Response(content=data, media_type="image/jpeg")



def _resolve_display_playlist(db: Session, display_id: str) -> Optional[str]:
    """The playlist a display should show when it didn't ask for one, by name, or None.

    Same precedence the Canvas gets from /api/displays/{id}/preferred-playlist — last-played for THIS
    display, then the global default — but with a final fallback the pull path specifically needs: the
    first playlist that actually HAS artwork. Ordering by id alone picks empty starter galleries on a
    fresh install and strands the device (see get_display_image).
    """
    last = db.query(SettingsModel).filter(SettingsModel.setting_key == f"last_playlist:{display_id}").first()
    default = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    name = (_playlist_name_if_playable(db, last.setting_value if last else None)
            or _playlist_name_if_playable(db, default.setting_value if default else None))
    if name:
        return name
    for p in db.query(PlaylistModel).order_by(PlaylistModel.id).all():
        if _playlist_name_if_playable(db, p.name):
            return p.name
    return None


@router.get("/api/displays/{display_id}/preferred-playlist")
async def get_preferred_playlist(display_id: str, db: Session = Depends(get_db)):
    """Which playlist a freshly-loaded display (no ?playlist= given) should show. Precedence:
    last-played for THIS display → the global `default_playlist` fallback → null (Canvas then picks the
    first non-empty). Only ever returns a playlist that still exists and has art."""
    display_id = normalize_display_id(display_id)
    # In demo mode, skip the "last played" lookup entirely — every display shares the "demo" id, so
    # this would otherwise let one visitor's gallery switch win for everyone (core/playback.py never
    # writes it in demo mode either; this is the read-side half of that same guarantee).
    last = None if config.DEMO_MODE else db.query(SettingsModel).filter(
        SettingsModel.setting_key == f"last_playlist:{display_id}").first()
    default = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    name = (_playlist_name_if_playable(db, last.setting_value if last else None)
            or _playlist_name_if_playable(db, default.setting_value if default else None))
    return {"playlist": name}

# --- R1-F2: Night & Quiet Hours (clock-driven brightness/warmth + quiet-hours panel power) ----------
# The settings routes (GET/POST /api/settings/display-schedule) now live in routers/settings.py; the
# resolver (resolve_schedule_state), its minute-math helpers (_parse_hhmm/_cyc_*), DEFAULT_SCHEDULE,
# SCHEDULE_SETTING_KEY, and _HHMM_RE all live in core/settings_util.py (imported above) — this
# schedule-state route is display-domain (not settings) and is the one caller left here.


@router.get("/api/displays/{display_id}/schedule-state")
async def get_schedule_state(display_id: str, now: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """The display's current brightness/warmth/quiet, resolved server-side from the wall clock. The Canvas
    polls this (~60s) and applies a CSS overlay; the appliance CEC timer polls it for panel power. `now`
    (HH:MM) overrides the clock for testing / filming the warm-shift time-lapse without waiting for night."""
    normalize_display_id(display_id)  # schedule state is global (unused below) — called anyway for
    # parity with the other display-scoped routes, see normalize_display_id's docstring.
    when = datetime.now()
    if now:
        m = _parse_hhmm(now, -1)
        if m < 0 or not _HHMM_RE.match(now):
            raise HTTPException(400, detail="now must be HH:MM")
        when = when.replace(hour=m // 60, minute=m % 60)
    return resolve_schedule_state(_load_schedule(db), when)


@router.get("/next-image")
async def get_next_image(
    playlist_name: str,
    shuffle: Optional[bool] = Query(None),
    display_id: str = Query("default"),
    direction: int = Query(1),
    db: Session = Depends(get_db)
):
    """Stateful next-image selection — thin route over core.playback.select_next_image."""
    display_id = normalize_display_id(display_id)
    return await select_next_image(playlist_name, shuffle, display_id, direction, db)


@router.get("/display/{display_id}/current.{ext}")
async def get_display_image(
    display_id: str,
    ext: str,
    playlist: Optional[str] = Query(None),
    w: int = Query(1600, ge=16, le=4096),
    h: int = Query(1200, ge=16, le=4096),
    palette: str = Query("spectra6"),
    fit: str = Query("cover"),
    shuffle: Optional[bool] = Query(None),
    interval: Optional[int] = Query(
        None, ge=15, le=86400,
        description="The client's own actual pull cadence in seconds (ADR-121), e.g. "
                     "max(display_time, EINK_MIN_INTERVAL) for the e-ink client. Widens how long "
                     "known_displays() keeps this display listed, so a client whose real sleep floor "
                     "is longer than 2x the playlist's display_time doesn't drop off /api/remote/displays "
                     "between its own pulls."),
):
    """
    Track B: stateless pull-on-wake image for e-ink / BYOS frames.

    Reuses /next-image's selection (advancing the same bag-shuffle), then renders
    the chosen artwork cropped to w x h and Floyd–Steinberg-dithered to the device
    palette. Returns the bytes plus an `X-Refresh-After` header (the playlist's
    display_time) so the frame knows how long to deep-sleep. No WebSocket, no JS.

    M6 (INFRA-D075 incident): no Depends(get_db) — the selection/lookup work below needs a session,
    but the render (run_image_work → render_for_epaper) does not, so it runs with the session already
    closed. touch_active_display gets its own short session afterward. See routers/library.py's
    thumbnail endpoint for the incident this pattern guards against.
    """
    ext = ext.lower()
    if ext not in VALID_FORMATS:
        raise HTTPException(404, detail="Use .png or .bmp")
    if palette not in PALETTES:
        raise HTTPException(400, detail=f"Unknown palette. Options: {', '.join(PALETTES)}")

    with SessionLocal() as db:
        # Playlist binding is stateless (v1): explicit ?playlist=, else resolve one that can actually be
        # PLAYED. This used to take `ORDER BY id LIMIT 1`, which is wrong the moment any empty playlist
        # sorts first — and on a fresh out-of-box install it always does: seeding creates several empty
        # starter galleries (ids 1-3) before the downloaded pack lands (id 4). The e-ink pull therefore
        # 404'd forever on a brand-new device while /playlists and /artworks both looked perfectly healthy,
        # and the panel just held its last frame. Found on the first real .img flash, 2026-07-21.
        if not playlist:
            playlist = _resolve_display_playlist(db, display_id)
            if not playlist:
                raise HTTPException(404, detail="No playlist with any artwork yet")

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
        aspect_crops, focal_x, focal_y = art.aspect_crops, art.focal_x, art.focal_y

    try:
        # A1: crop + enhance + Floyd–Steinberg dither + encode is heavy and blocking — thread it so an
        # e-ink cache miss doesn't stall the worker loop (frame_push threads its sibling render likewise).
        # Prefer an authored per-shape crop over the focal cover. Picked against the REQUESTED w/h,
        # so a portrait-hung panel asking for 1200x1600 gets the portrait composition, not a
        # landscape one. None (no crop data) => unchanged focal-cover behaviour.
        crop_box = pick_crop_for_aspect(aspect_crops, w, h)
        data = await run_image_work(render_for_epaper, path, w, h, palette=palette, fit=fit,
                                     focal=(focal_x, focal_y), fmt=ext,
                                     crop_box=crop_box)
    except Exception as e:
        logger.error(f"[epaper] render failed for {path.name}: {e}", exc_info=True)
        raise HTTPException(500, detail="Render failed")

    with SessionLocal() as db:
        touch_active_display(db, display_id, refresh_s=interval)

    return Response(
        content=data,
        media_type=media_type_for(ext),
        headers={
            "X-Refresh-After": str(info["display_time"]),
            # Content hash so the e-ink pull client can change-detect a repaint without a
            # ~30s panel refresh on an unchanged frame (eink_client dedupes on this; it falls
            # back to hashing the body if the header is ever absent).
            "ETag": '"' + hashlib.sha256(data).hexdigest()[:16] + '"',
            # M6 nit: no explicit Cache-Control here — app.py's CacheHeadersMiddleware already forces
            # "no-store, no-cache, must-revalidate" on every /display/* path; setting it here too gave
            # the response two (identical, harmless, but sloppy) Cache-Control headers.
        },
    )


class TelemetryHeartbeat(BaseModel):
    artwork_id: int
    display_time_sec: int
    skipped: bool

@router.post("/api/telemetry/heartbeat")
def record_telemetry(payload: TelemetryHeartbeat, db: Session = Depends(get_db)):
    """
    Phase 6: Ingests display metrics from Canvas clients.
    """
    if config.DEMO_MODE:
        # Allowed through the demo gate so the Canvas doesn't error, but a no-op — no DB write for an
        # anonymous visitor's telemetry (core/demo.py §1).
        return Response(status_code=204)
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
