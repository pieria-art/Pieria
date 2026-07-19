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

from config import LIBRARY_DIR
from core.media import render_canvas_image
from core.playback import _playlist_name_if_playable, select_next_image, touch_active_display
from core.settings_util import _HHMM_RE, _load_schedule, _parse_hhmm, resolve_schedule_state
from database import get_db
from epaper import PALETTES, VALID_FORMATS, media_type_for, render_for_epaper
from models import ArtworkModel, PlaylistModel, SettingsModel

logger = logging.getLogger("artwork-display-api")

router = APIRouter()


@router.get("/artworks/{artwork_id}/display.jpg")
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



@router.get("/api/displays/{display_id}/preferred-playlist")
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


@router.get("/api/displays/{display_id}/schedule-state")
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


@router.get("/next-image")
async def get_next_image(
    playlist_name: str,
    shuffle: Optional[bool] = Query(None),
    display_id: str = Query("default"),
    direction: int = Query(1),
    db: Session = Depends(get_db)
):
    """Stateful next-image selection — thin route over core.playback.select_next_image."""
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
            # Content hash so the e-ink pull client can change-detect a repaint without a
            # ~30s panel refresh on an unchanged frame (eink_client dedupes on this; it falls
            # back to hashing the body if the header is ever absent).
            "ETag": '"' + hashlib.sha256(data).hexdigest()[:16] + '"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
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
