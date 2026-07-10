"""Playback selection + now-playing/liveness helpers.

`select_next_image` is the canonical bag-shuffle/affinity selection brain. It was
previously the body of the `GET /next-image` route, but it is *also* invoked in-process
by the e-ink pull route and the Frame-TV pusher — so it lives here as a plain callable
and the route is a thin wrapper. The now-playing + liveness helpers are shared by the
display, ws/remote, and health domains.
"""

import json
import logging
import random
from datetime import UTC, datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import frame_push
from config import LIBRARY_DIR
from database import SessionLocal
from models import (
    ActiveDisplayModel,
    ArtworkModel,
    DisplayPlaybackSessionModel,
    PlaylistModel,
    SettingsModel,
    playlist_artwork,
)

logger = logging.getLogger("artwork-display-api")


async def select_next_image(
    playlist_name: str,
    shuffle: Optional[bool],
    display_id: str,
    direction: int,
    db: Session,
) -> dict:
    """Stateful next-image selection (Phase 6).

    Uses 'bag shuffle' for variety and persists state per display. The canonical
    selection brain: the `/next-image` route, the e-ink pull route, and the Frame
    pusher all flow through here so selection logic exists exactly once.
    """
    p = db.query(PlaylistModel).filter(PlaylistModel.name == playlist_name).first()
    if not p: raise HTTPException(404)

    # Remember the active playlist for this display so a reboot resumes it (not the first playlist).
    # Guarded so it only writes on change; rides the session-state commit below.
    if display_id and display_id != "default":
        _lp_key = f"last_playlist:{display_id}"
        _lp_row = db.query(SettingsModel).filter(SettingsModel.setting_key == _lp_key).first()
        if _lp_row is None:
            db.add(SettingsModel(setting_key=_lp_key, setting_value=playlist_name))
        elif _lp_row.setting_value != playlist_name:
            _lp_row.setting_value = playlist_name

    # Resolve Shuffle Hierarchy (URL override > Playlist setting)
    resolved_shuffle = shuffle if shuffle is not None else p.shuffle

    # Fetch all approved artworks in this playlist
    artworks = db.query(ArtworkModel).join(playlist_artwork).filter(
        playlist_artwork.c.playlist_id == p.id,
        ArtworkModel.status == 'approved'
    ).order_by(playlist_artwork.c.display_order).all()

    if not artworks: raise HTTPException(404, detail="No approved images")
    count = len(artworks)

    # Get or create playback session. A8: the (display_id, playlist_id) UNIQUE constraint backstops the
    # check-then-insert race across the 4 workers — if another worker inserts first, catch the
    # IntegrityError, roll back, and re-query the row it created instead of duplicating it.
    def _get_session():
        return db.query(DisplayPlaybackSessionModel).filter(
            DisplayPlaybackSessionModel.display_id == display_id,
            DisplayPlaybackSessionModel.playlist_id == p.id
        ).first()

    session = _get_session()
    if not session:
        session = DisplayPlaybackSessionModel(display_id=display_id, playlist_id=p.id)
        db.add(session)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            session = _get_session()

    selected_art = None
    selected_idx = -1

    if resolved_shuffle:
        # Bag Shuffle Logic
        unplayed_ids = json.loads(session.unplayed_artworks_json)

        # Valid approved IDs in this playlist
        valid_ids = [a.id for a in artworks]

        # Filter unplayed to only include currently valid/approved IDs
        bag = [aid for aid in unplayed_ids if aid in valid_ids]

        # If bag is empty, refill it
        if not bag:
            bag = valid_ids
            logger.info(f"[Director] Refilling bag for display '{display_id}' / playlist '{playlist_name}'")

        # Phase 6 Bonus: Weighted random draw based on affinity_score
        # Get actual artwork objects for the IDs in the bag to access affinity scores
        bag_artworks = [a for a in artworks if a.id in bag]

        if bag_artworks:
            # random.choices uses weights. affinity_score defaults to 1.0.
            weights = [max(0.1, a.affinity_score) for a in bag_artworks]
            selected_art = random.choices(bag_artworks, weights=weights, k=1)[0]

            # Remove from bag
            bag.remove(selected_art.id)
            session.unplayed_artworks_json = json.dumps(bag)

            # Find its index in the ordered list for the frontend (optional but helpful)
            for i, a in enumerate(artworks):
                if a.id == selected_art.id:
                    selected_idx = i
                    break
    else:
        # Stateful Sequential Logic
        base_idx = session.last_sequential_index
        selected_idx = (base_idx + direction) % count
        selected_art = artworks[selected_idx]
        session.last_sequential_index = selected_idx

    db.commit()

    # Now-playing: record what this display is showing so /remote + Devices can surface it. Covers
    # both Canvas (calls this route) and e-ink (calls it via get_display_image). Own commit; liveness
    # stays heartbeat-owned so a fresh selection never masks a display that stopped checking in.
    _record_now_playing(db, display_id, selected_art.id, playlist_name)

    try:
        _ver = int((LIBRARY_DIR / selected_art.filename).stat().st_mtime)
    except OSError:
        _ver = 0

    return {
        "index": selected_idx,
        # Resolution-capped derivative (not the full-res original) so a Pi-class browser
        # can actually decode/paint it; ?v=mtime busts the immutable cache on re-crop/replace.
        "image_url": f"/artworks/{selected_art.id}/display.jpg?v={_ver}",
        "playlist": playlist_name,
        "display_time": p.display_time,
        "default_mode": p.default_mode,
        "shuffle": resolved_shuffle,
        "placard_wait": p.placard_initial_wait_sec,
        "placard_show": p.placard_initial_show_sec,
        "placard_manual": p.placard_interaction_show_sec,
        "crop": {"x": selected_art.crop_x, "y": selected_art.crop_y, "width": selected_art.crop_width, "height": selected_art.crop_height},
        "focal_point": {"x": selected_art.focal_x, "y": selected_art.focal_y},
        "metadata": {
            "id": selected_art.id,
            "is_personal": selected_art.is_personal,
            "title": selected_art.title, "agent_name": selected_art.agent_name, "agent_role": selected_art.agent_role,
            "creation_date": selected_art.creation_date, "cultural_context": selected_art.cultural_context,
            "medium": selected_art.medium, "date_display": selected_art.date_display,
            "description": selected_art.description_narrative, "tags": selected_art.tags
        }
    }


def _record_now_playing(db: Session, display_id: str, artwork_id: int, playlist_name: str):
    """Persist the artwork/collection a display is currently showing. Upserts only the current_* fields;
    last_seen_at (liveness) is owned by the WS heartbeat / touch_active_display, so updating now-playing
    never revives a display that stopped checking in. Best-effort — a failure here must not break a serve."""
    try:
        d = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()
        if d:
            d.current_artwork_id = artwork_id
            d.current_playlist = playlist_name
        else:
            db.add(ActiveDisplayModel(display_id=display_id, current_artwork_id=artwork_id,
                                      current_playlist=playlist_name))
        db.commit()
    except Exception as e:
        logger.error(f"_record_now_playing error for {display_id}: {e}", exc_info=True)
        db.rollback()


def _now_playing_artwork(db: Session, artwork_id: Optional[int]) -> Optional[dict]:
    """Compact card for the artwork currently on a display (None if unknown/deleted)."""
    if not artwork_id:
        return None
    a = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not a:
        return None
    return {"id": a.id, "title": a.title, "agent_name": a.agent_name,
            "is_personal": a.is_personal, "thumb_url": f"/artworks/{a.id}/thumbnail"}


def _display_now_playing(db: Session, row: "ActiveDisplayModel") -> dict:
    """{display_id, playlist, artwork} for a display row — the shared shape for /remote + Devices."""
    return {"display_id": row.display_id, "playlist": row.current_playlist,
            "artwork": _now_playing_artwork(db, row.current_artwork_id)}


def touch_active_display(db: Session, display_id: str):
    """Upsert last_seen_at so pull-on-wake e-ink frames show up in the remote/
    admin just like WebSocket-connected Canvas displays."""
    try:
        d = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()
        if d:
            d.last_seen_at = datetime.now(UTC)
        else:
            db.add(ActiveDisplayModel(display_id=display_id))
        db.commit()
    except Exception as e:
        logger.error(f"touch_active_display error for {display_id}: {e}")
        db.rollback()


def _playlist_name_if_playable(db: Session, name: Optional[str]) -> Optional[str]:
    """Return `name` only if that playlist still exists AND has at least one artwork; else None."""
    if not name:
        return None
    pl = db.query(PlaylistModel).filter(PlaylistModel.name == name).first()
    return name if (pl and len(pl.artworks) > 0) else None


async def _frame_select(playlist: str):
    """Selector injected into the Frame pusher: pick the current artwork for a playlist (reusing the
    bag-shuffle/affinity in get_next_image, on a dedicated display_id) and return (file_path, id).

    Lives here (not a router) so `core.lifespan`'s boot task can start `frame_push.frame_push_loop`
    without importing a router, and so `routers/settings.py`'s "Test / Push now" route can reuse the
    same selector without importing app.py."""
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
