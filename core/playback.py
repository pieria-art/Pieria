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
import threading
from datetime import UTC, datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import config
import core.licensing as core_licensing
import frame_push
from config import LIBRARY_DIR
from database import SessionLocal
from models import (
    ActiveDisplayModel,
    ArtworkModel,
    DisplayPlaybackSessionModel,
    PlaylistModel,
    RemoteCommandModel,
    SettingsModel,
    playlist_artwork,
)

logger = logging.getLogger("artwork-display-api")


def placard_metadata(art: ArtworkModel) -> dict:
    """The canonical placard payload for one artwork.

    ONE definition, so the TV placard (/next-image -> app.js updatePlacard) and the phone placard
    (/artworks/{id}/placard -> remote.html) cannot drift apart. tests/test_placard_api.py asserts the
    two key sets are equal precisely so that adding a field to one and not the other fails loudly.

    Returns RAW model values — Markdown stripping is the caller's job, because the two callers strip in
    different places: the Canvas does it client-side in stripMd() (static/app.js), the phone endpoint
    does it server-side with config.strip_markdown().

    Attribution fields (ADR-142 Stage B): `license_name` is the display name (core.licensing.
    LICENSE_NAMES), `requires_attribution` is True only for CC BY 4.0 — the two placard surfaces (Canvas,
    remote.html) use it to decide whether to show the credit line at all (decision B: PD/CC0 works show
    nothing new on the placard).
    """
    license_id = core_licensing.normalize_license(art.license)
    return {
        "id": art.id,
        "is_personal": art.is_personal,
        "title": art.title, "agent_name": art.agent_name, "agent_role": art.agent_role,
        "creation_date": art.creation_date, "cultural_context": art.cultural_context,
        "medium": art.medium, "date_display": art.date_display,
        "series": art.series,
        "description": art.description_narrative, "tags": art.tags,
        "license": license_id,
        "license_name": core_licensing.LICENSE_NAMES.get(license_id) if license_id else None,
        "license_url": art.license_url,
        "attribution": art.attribution,
        "attribution_url": art.attribution_url,
        "origin_url": art.origin_url,
        "requires_attribution": core_licensing.requires_attribution(license_id),
    }


# ADR-148 (race fix): selection/now-playing routes now run in the threadpool, so several requests for the
# SAME display can interleave inside one worker (they used to serialize on the event loop). Two defences:
#  1. every Settings-KV / active_displays INSERT on these hot paths is an atomic SQLite upsert, so a
#     check-then-insert race can never surface as a UNIQUE-constraint 500 (or a silently lost touch);
#  2. selection is serialized per display_id inside the process by a STRIPED lock (fixed size, so an
#     unauthenticated caller inventing display_ids cannot grow it). Cross-worker races remain the
#     pre-existing baseline — the upserts are what keeps them from failing.
_SELECT_LOCKS = [threading.Lock() for _ in range(64)]


def _select_lock(display_id: str) -> threading.Lock:
    return _SELECT_LOCKS[hash(display_id) % len(_SELECT_LOCKS)]


def _kv_upsert(db: Session, key: str, value: str) -> None:
    """Atomic `INSERT ... ON CONFLICT(setting_key) DO UPDATE` for a Settings-KV row. Rides the caller's
    commit. (Core statement: ORM objects already loaded for this key are not refreshed.)"""
    db.execute(sqlite_insert(SettingsModel).values(setting_key=key, setting_value=value)
               .on_conflict_do_update(index_elements=["setting_key"], set_={"setting_value": value}))


async def select_next_image(*args, **kwargs) -> dict:
    """Async-compatible wrapper over `select_next_image_sync` (kept for existing `await` callers, e.g.
    the Frame pusher). The selection body never awaits anything — it is pure sync DB work — so routes
    should call `select_next_image_sync` from a plain `def` route / `run_in_threadpool` (ADR-148)."""
    return select_next_image_sync(*args, **kwargs)


def select_next_image_sync(*args, **kwargs) -> dict:
    """Per-display serialized entry point (see _select_lock). Signature: see _select_next_image_locked."""
    display_id = kwargs.get("display_id", args[2] if len(args) > 2 else "")
    with _select_lock(display_id):
        return _select_next_image_locked(*args, **kwargs)


def _select_next_image_locked(
    playlist_name: str,
    shuffle: Optional[bool],
    display_id: str,
    direction: int,
    db: Session,
    manual: bool = False,
    show_artwork_id: Optional[int] = None,
) -> dict:
    """Stateful next-image selection (Phase 6).

    A2 (ADR-147): three things can short-circuit the bag/sequence advance, in this order —
      1. `show_artwork_id` (Canvas "show artwork now"): serve that approved artwork (404 if unknown).
      2. a pending show-next set through the public API for a pull-on-wake display (consumed once).
      3. PAUSE: a paused display's auto-advance (`direction=1`, `manual=False`) re-serves the item it is
         already showing instead of advancing. A manual advance (`manual=True`, or any direction != 1)
         still advances exactly once and the display stays paused.
    The response carries `paused` so a Canvas that reloads while paused does not restart its timer.

    Uses 'bag shuffle' for variety and persists state per display. The canonical
    selection brain: the `/next-image` route, the e-ink pull route, and the Frame
    pusher all flow through here so selection logic exists exactly once.
    """
    p = db.query(PlaylistModel).filter(PlaylistModel.name == playlist_name).first()
    if not p: raise HTTPException(404)

    # Remember the active playlist for this display so a reboot resumes it (not the first playlist).
    # Guarded so it only writes on change; rides the session-state commit below. Skipped in demo mode:
    # every display collapses to the shared "demo" id (normalize_display_id), so writing this would
    # let one visitor's gallery switch silently override SD_DEMO_DEFAULT_PLAYLIST for every other
    # visitor — the default must keep winning for everyone, always.
    if display_id and display_id != "default" and not config.DEMO_MODE:
        _lp_key = f"last_playlist:{display_id}"
        _lp_row = db.query(SettingsModel).filter(SettingsModel.setting_key == _lp_key).first()
        if _lp_row is None:
            _kv_upsert(db, _lp_key, playlist_name)
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

    def _ensure_session():
        session = _get_session()
        if not session:
            session = DisplayPlaybackSessionModel(display_id=display_id, playlist_id=p.id)
            db.add(session)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                session = _get_session()
        return session

    paused = is_display_paused(db, display_id)
    held = None
    moved = False   # a show (not a pause re-serve) repositions the rotation cursor
    if show_artwork_id is not None:
        held = db.query(ArtworkModel).filter(ArtworkModel.id == show_artwork_id,
                                             ArtworkModel.status == 'approved').first()
        if held is None:
            raise HTTPException(404, detail="No such artwork")
    if held is None:
        held = _take_pending_show(db, display_id)
    moved = held is not None
    if held is None and paused and direction == 1 and not manual:
        # The hold target lives in the pause flag itself: the active_displays row is deleted on a WS
        # disconnect and recreated empty on reconnect, so it cannot be trusted across a Canvas reload.
        hold = _pause_hold(db, display_id)
        if hold and hold.get("playlist") == playlist_name and hold.get("artwork_id"):
            held = db.query(ArtworkModel).filter(ArtworkModel.id == hold["artwork_id"],
                                                 ArtworkModel.status == 'approved').first()
    if held is not None:
        db.commit()   # the last_playlist write above rides this
        idx = next((i for i, a in enumerate(artworks) if a.id == held.id), -1)
        if moved and idx >= 0:
            # Spec §2: show-now "then continues the rotation from there". Shown item is in the current
            # playlist -> sequential cursor jumps to it; shuffle drops it from the remaining bag so it
            # is not repeated. Not in the playlist -> cursor/bag untouched (rotation resumes where it was).
            session = _ensure_session()
            if resolved_shuffle:
                valid_ids = [a.id for a in artworks]
                bag = [x for x in json.loads(session.unplayed_artworks_json) if x in valid_ids] or valid_ids
                session.unplayed_artworks_json = json.dumps([x for x in bag if x != held.id])
            else:
                session.last_sequential_index = idx
            db.commit()
        _record_now_playing(db, display_id, held.id, playlist_name)
        return _selection_payload(held, idx, p, resolved_shuffle, playlist_name, paused)

    session = _ensure_session()

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

    return _selection_payload(selected_art, selected_idx, p, resolved_shuffle, playlist_name, paused)


def _selection_payload(selected_art, selected_idx: int, p, resolved_shuffle: bool, playlist_name: str,
                       paused: bool) -> dict:
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
        # Per-aspect crop presets (may be None). The CLIENT picks by its own viewport ratio — the
        # server can't, since one now-playing payload fans out to displays of different shapes.
        "aspect_crops": selected_art.aspect_crops,
        "metadata": placard_metadata(selected_art),
        "paused": paused,
    }


def _record_now_playing(db: Session, display_id: str, artwork_id: int, playlist_name: str):
    """Persist the artwork/collection a display is currently showing. Upserts only the current_* fields;
    last_seen_at (liveness) is owned by the WS heartbeat / touch_active_display, so updating now-playing
    never revives a display that stopped checking in. Best-effort — a failure here must not break a serve."""
    try:
        d = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()
        if d:
            if d.current_artwork_id != artwork_id or d.current_playlist != playlist_name:
                touch_display_updated(db, display_id)
            d.current_artwork_id = artwork_id
            d.current_playlist = playlist_name
        else:
            touch_display_updated(db, display_id)
            db.execute(sqlite_insert(ActiveDisplayModel).values(
                display_id=display_id, current_artwork_id=artwork_id, current_playlist=playlist_name,
                last_seen_at=datetime.now(UTC))
                .on_conflict_do_update(index_elements=["display_id"],
                                       set_={"current_artwork_id": artwork_id,
                                             "current_playlist": playlist_name}))
        _refresh_pause_hold(db, display_id, artwork_id, playlist_name)
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


#: A queued command older than this is never delivered and is purged (A2, ADR-147). A command is a
#: "do it now" intent for a display that is live — one that sat longer (display offline, tab asleep)
#: would otherwise fire minutes later as a surprise, or queue forever.
COMMAND_TTL_SEC = 60


def _command_cutoff() -> datetime:
    # remote_commands.created_at is stored as UTC wall-clock without tzinfo (SQLite), so compare naive.
    return (datetime.now(UTC) - timedelta(seconds=COMMAND_TTL_SEC)).replace(tzinfo=None)


def queue_remote_command(db: Session, display_id: str, action: str,
                         playlist: Optional[str] = None, mode: Optional[str] = None,
                         artwork_id: Optional[int] = None) -> None:
    """Persist a command for a display's WS poller to relay (the Phase 5 cross-worker bridge — see
    routers/ws.py). The ONE writer of remote_commands: `/api/remote/change` and the public API's
    `/api/v1/displays/{id}/commands` both go through here, so the payload shape the Canvas parses
    (`{action, playlist?, mode?, artwork_id?}`) cannot diverge between them. Expired rows (older than
    COMMAND_TTL_SEC) are purged on every enqueue."""
    payload = {"action": action}
    if playlist:
        payload["playlist"] = playlist
    if mode:
        payload["mode"] = mode
    if artwork_id is not None:
        payload["artwork_id"] = artwork_id
    db.query(RemoteCommandModel).filter(RemoteCommandModel.created_at < _command_cutoff()).delete(
        synchronize_session=False)
    db.add(RemoteCommandModel(target_display=display_id, action=action, payload=json.dumps(payload)))
    db.commit()


def queue_schedule_refresh(db: Session) -> int:
    """After a quiet change, tell every LIVE canvas display to re-poll schedule-state NOW instead of at its
    next 60 s tick (so a blackout lands in ~1-2 s). Pull-based e-ink/Frame have no live channel and are
    skipped; so are non-live rows (the 60 s command TTL would expire it before delivery anyway). Returns
    the number of displays queued. Best-effort: a failure here must never fail the quiet change itself."""
    n = 0
    try:
        now = datetime.now(UTC)
        for row in db.query(ActiveDisplayModel).filter(ActiveDisplayModel.kind == "canvas").all():
            if _is_live(row, now):
                queue_remote_command(db, row.display_id, "refresh_schedule")
                n += 1
    except Exception:
        logger.warning("queue_schedule_refresh failed", exc_info=True)
        db.rollback()
    return n


def take_deliverable_commands(db: Session, display_id: str) -> list[dict]:
    """Pop this display's queued commands for the WS relay: returns the payloads of the fresh ones (in
    order) and deletes every row it saw, so an EXPIRED command (older than COMMAND_TTL_SEC) is purged
    and never delivered."""
    cutoff = _command_cutoff()
    rows = (db.query(RemoteCommandModel).filter(RemoteCommandModel.target_display == display_id)
              .order_by(RemoteCommandModel.id).all())
    out = []
    for cmd in rows:
        created = cmd.created_at.replace(tzinfo=None) if cmd.created_at else None
        if created is None or created >= cutoff:
            out.append(json.loads(cmd.payload))
        db.delete(cmd)
    db.commit()
    return out


# --- Pause + show-next (A2, ADR-147): per-display flags in the Settings KV ------------------------------
# KV rather than a column on active_displays/display_playback_sessions: the active_displays row is DELETED
# on every clean WS disconnect (a Canvas reload would silently un-pause) and playback sessions are per
# (display, playlist). Absent row = not paused.
_PAUSED_PREFIX = "display_paused:"
_SHOW_NEXT_PREFIX = "display_show_next:"
#: A queued show-next for a pull-on-wake display waits for its next pull — which may be minutes away.
SHOW_NEXT_TTL_SEC = 6 * 3600


def is_display_paused(db: Session, display_id: str) -> bool:
    return db.query(SettingsModel.setting_key).filter(
        SettingsModel.setting_key == _PAUSED_PREFIX + display_id).first() is not None


def _pause_hold(db: Session, display_id: str) -> Optional[dict]:
    """The {artwork_id, playlist} a paused display is holding, or None (not paused / not recorded yet)."""
    row = db.query(SettingsModel.setting_value).filter(
        SettingsModel.setting_key == _PAUSED_PREFIX + display_id).first()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def _refresh_pause_hold(db: Session, display_id: str, artwork_id: int, playlist_name: str) -> None:
    """While paused, whatever the display now shows (incl. after an explicit next/show) is what it holds.
    Rides the caller's commit."""
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == _PAUSED_PREFIX + display_id).first()
    if row is not None:
        row.setting_value = json.dumps({"artwork_id": artwork_id, "playlist": playlist_name})


def set_display_paused(db: Session, display_id: str, paused: bool) -> None:
    key = _PAUSED_PREFIX + display_id
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == key).first()
    if paused and row is None:
        cur = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()
        hold = ({"artwork_id": cur.current_artwork_id, "playlist": cur.current_playlist}
                if cur and cur.current_artwork_id else {})
        _kv_upsert(db, key, json.dumps(hold))
        touch_display_updated(db, display_id)
    elif not paused and row is not None:
        db.delete(row)
        touch_display_updated(db, display_id)
    db.commit()


_UPDATED_PREFIX = "display_updated:"


def touch_display_updated(db: Session, display_id: str, force: bool = False) -> None:
    """Stamp "this display's now-playing / paused / mode just changed" (public API `updated_at`, so a
    client can tell a queued command has taken effect). Rides the caller's commit."""
    if not force and db.query(ActiveDisplayModel.display_id).filter(
            ActiveDisplayModel.display_id == display_id).first() is None:
        return   # /next-image is unauthenticated: never grow a KV row per arbitrary display_id
    key = _UPDATED_PREFIX + display_id
    value = datetime.now(UTC).isoformat()
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == key).first()
    if row is None:
        _kv_upsert(db, key, value)
    else:
        row.setting_value = value


def display_updated_at(db: Session, display_id: str) -> Optional[datetime]:
    row = db.query(SettingsModel.setting_value).filter(
        SettingsModel.setting_key == _UPDATED_PREFIX + display_id).first()
    if row is None:
        return None
    try:
        return _as_utc(datetime.fromisoformat(row[0]))
    except (ValueError, TypeError):
        return None


def set_pending_show(db: Session, display_id: str, artwork_id: int, ttl_sec: int = SHOW_NEXT_TTL_SEC) -> None:
    """Make `artwork_id` the next item this display is served (consumed by select_next_image)."""
    key = _SHOW_NEXT_PREFIX + display_id
    value = json.dumps({"id": artwork_id, "exp": (datetime.now(UTC) + timedelta(seconds=ttl_sec)).timestamp()})
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == key).first()
    if row is None:
        _kv_upsert(db, key, value)
    else:
        row.setting_value = value
    db.commit()


def _take_pending_show(db: Session, display_id: str) -> Optional["ArtworkModel"]:
    """Consume the pending show-next, if any and unexpired and still approved."""
    key = _SHOW_NEXT_PREFIX + display_id
    # Atomic consume-once: DELETE ... RETURNING hands the value to exactly one caller, even across threads
    # or workers (a read-then-delete would let two concurrent pulls both serve the show-next).
    taken = db.execute(delete(SettingsModel).where(SettingsModel.setting_key == key)
                       .returning(SettingsModel.setting_value)).first()
    db.commit()
    if taken is None:
        return None
    try:
        data = json.loads(taken[0])
        ok = float(data["exp"]) > datetime.now(UTC).timestamp()
        art_id = int(data["id"])
    except (ValueError, KeyError, TypeError):
        ok, art_id = False, 0
    if not ok:
        return None
    return db.query(ArtworkModel).filter(ArtworkModel.id == art_id, ArtworkModel.status == 'approved').first()


def _display_now_playing(db: Session, row: "ActiveDisplayModel") -> dict:
    """{display_id, playlist, artwork} for a display row — the shared shape for /remote + Devices."""
    return {"display_id": row.display_id, "playlist": row.current_playlist,
            "artwork": _now_playing_artwork(db, row.current_artwork_id)}


# --- Liveness windows -------------------------------------------------------------------------
# Two different questions, two different windows:
#   LIVE  — "is this display checking in right now?" A Canvas heartbeat lands every 5s (ws.py), so
#           15s is three missed beats. This is what gates the remote-control command surface.
#   KNOWN — "should the remote still remember this display?" An e-ink panel pulls one frame and then
#           DEEP-SLEEPS for the playlist's display_time, so a strict 15s window makes the very display
#           you want the placard for invisible almost all the time. The window is derived from the
#           panel's own cadence (2x display_time = one missed pull plus slack) — server-side arithmetic
#           only. Nothing here keeps a panel awake or reaches out to it: the frame decides when to wake
#           from the X-Refresh-After header, which matters for future battery-powered panels.
LIVE_WINDOW_SEC = 15
# Ceiling. touch_active_display() rows are never garbage-collected (only a clean WS disconnect deletes
# one), so without this a future daily-refresh panel would linger in the dropdown for days after being
# unplugged.
MAX_KNOWN_WINDOW_SEC = 6 * 3600
#: SettingsModel KV key prefix for a display's OWN reported pull cadence (ADR-121). The e-ink client's
#: actual sleep is max(display_time, EINK_MIN_INTERVAL) — a client-side floor the server has no other
#: way to learn. A 30s playlist behind a 900s EINK_MIN_INTERVAL made the 2*display_time window (60s)
#: hide the panel for 14 of every 15 minutes (2026-09-20 bench incident) even though it was pulling
#: exactly on schedule. Stored as a plain KV row (no schema migration) alongside `last_playlist:<id>`.
_REFRESH_S_PREFIX = "display_refresh_s:"


def _as_utc(dt: datetime) -> datetime:
    """SQLite hands back last_seen_at NAIVE (the column is DateTime, not DateTime(timezone=True)) even
    though it was written aware, so comparing it to an aware now() raises TypeError. Harmless until a
    comparison happens in Python instead of SQL — which the per-row KNOWN window below does."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _is_live(row: "ActiveDisplayModel", now: datetime) -> bool:
    """Checking in right now — i.e. reachable by a remote command."""
    return _as_utc(row.last_seen_at) > now - timedelta(seconds=LIVE_WINDOW_SEC)


def known_displays(db: Session) -> list[tuple["ActiveDisplayModel", bool]]:
    """Displays the remote should still remember, each with whether it's currently live.

    One outer join rather than a per-row playlist lookup. current_playlist is a denormalized NAME, not
    an FK, and the rename path doesn't update it — so a renamed playlist misses the join and collapses
    to the LIVE window, which self-heals on that display's next frame serve. Same fallback covers a
    display that has checked in but never served (current_playlist is null).

    The window also considers each display's OWN reported pull cadence (`refresh_s`, ADR-121), not
    just 2x the playlist's display_time — a client whose actual sleep floor (EINK_MIN_INTERVAL) is
    longer than that must still be widened, or it goes KNOWN-window-invisible between its own pulls.
    """
    now = datetime.now(UTC)   # one clock for every row in the response
    rows = (db.query(ActiveDisplayModel, PlaylistModel.display_time)
              .outerjoin(PlaylistModel, PlaylistModel.name == ActiveDisplayModel.current_playlist)
              .all())
    refresh_by_display = {}
    if rows:
        settings_rows = (db.query(SettingsModel.setting_key, SettingsModel.setting_value)
                            .filter(SettingsModel.setting_key.like(f"{_REFRESH_S_PREFIX}%"))
                            .all())
        for key, value in settings_rows:
            refresh_by_display[key[len(_REFRESH_S_PREFIX):]] = value

    out = []
    for row, display_time in rows:
        refresh_s = 0
        raw = refresh_by_display.get(row.display_id)
        if raw is not None:
            try:
                refresh_s = int(raw)
            except (TypeError, ValueError):
                refresh_s = 0
        window = min(max(LIVE_WINDOW_SEC, 2 * (display_time or 0), 2 * refresh_s), MAX_KNOWN_WINDOW_SEC)
        if _as_utc(row.last_seen_at) > now - timedelta(seconds=window):
            out.append((row, _is_live(row, now)))
    return out


def touch_active_display(db: Session, display_id: str, refresh_s: Optional[int] = None):
    """Upsert last_seen_at so pull-on-wake e-ink frames show up in the remote/
    admin just like WebSocket-connected Canvas displays.

    `refresh_s` (ADR-121) is the client's own reported pull cadence — sent by the e-ink pull route as
    the `interval` query param — persisted into SettingsModel as `display_refresh_s:<display_id>` (no
    schema migration needed) so known_displays() can widen its window to match. None (Canvas, or an
    e-ink client that hasn't been updated yet) leaves any previously-stored value untouched.
    """
    try:
        # Atomic upserts (not query-then-add): concurrent pulls for one display must not lose the touch to
        # an IntegrityError that this handler would swallow. kind: only the e-ink pull route calls this;
        # the public API reports it (ADR-147).
        now = datetime.now(UTC)
        db.execute(sqlite_insert(ActiveDisplayModel).values(display_id=display_id, kind="eink", last_seen_at=now)
                   .on_conflict_do_update(index_elements=["display_id"],
                                          set_={"last_seen_at": now, "kind": "eink"}))
        if refresh_s is not None:
            _kv_upsert(db, f"{_REFRESH_S_PREFIX}{display_id}", str(refresh_s))
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


def _frame_select_sync(playlist: str):
    """Selector injected into the Frame pusher: pick the current artwork for a playlist (reusing the
    bag-shuffle/affinity in get_next_image, on a dedicated display_id) and return (file_path, id, focal, aspect_crops).

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
        info = select_next_image_sync(
            playlist_name=pl, shuffle=None, display_id=cfg["display_id"], direction=1, db=db
        )
        art_id = (info.get("metadata") or {}).get("id")
        if not art_id:
            return None
        art = db.query(ArtworkModel).filter(ArtworkModel.id == art_id).first()
        if not art:
            return None
        return (LIBRARY_DIR / art.filename, art_id, (art.focal_x, art.focal_y), art.aspect_crops)
    except Exception as e:
        logger.warning(f"[Frame] selection failed: {e}")
        return None
    finally:
        db.close()


async def _frame_select(playlist: str):
    """Async facade for the Frame pusher / "Test / Push now": the selection is pure sync DB work, so it
    runs in a worker thread on its own short-lived session instead of blocking the event loop (ADR-148)."""
    from fastapi.concurrency import run_in_threadpool
    return await run_in_threadpool(_frame_select_sync, playlist)
