"""WebSocket + remote control — extracted from app.py (Phase 3 of the app-split refactor).

The targeted WebSocket registry (core.connections.manager) is shared with the remote-command push
path here: `/ws/{display_id}` accepts a display's socket, and `POST /api/remote/change` persists a
command the socket's own `command_poller` picks up and relays — bridging across Uvicorn's multiple
worker processes (ADR-006), since no single worker sees every display's socket.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import appliance_settings
from core.connections import manager
from core.playback import _display_now_playing, _is_live, known_displays
from core.security import _origin_allowed
from database import SessionLocal, get_db
from models import ActiveDisplayModel, RemoteCommandModel

logger = logging.getLogger("artwork-display-api")

router = APIRouter()


# -----------------------------------------------------------------------------
# 4. WebSocket & Remote Control
# -----------------------------------------------------------------------------
# GET /remote (the page) now lives in routers/pages.py.
@router.get("/api/remote/displays")
async def get_active_displays(db: Session = Depends(get_db)):
    """Displays the Remote should remember, each with what it's showing so the page can render the
    'now showing' panel + placard and highlight the active collection.
    Shape: {display_id, playlist, artwork, live}.

    Deliberately NOT limited to the 15s live window. An e-ink panel pulls one frame then deep-sleeps
    for the playlist's display_time, so a strict window hid the very display whose placard the phone
    is meant to carry. `live` still reports the strict window — it's what gates the command surface,
    since a sleeping panel holds no socket to receive a command on. See core.playback.known_displays.
    """
    return [{**_display_now_playing(db, row), "live": live} for row, live in known_displays(db)]


@router.get("/api/displays/{display_id}/now-playing")
async def get_display_now_playing(display_id: str, db: Session = Depends(get_db)):
    """What one display is currently showing (artwork + collection). artwork is null until the display
    has served a frame. `active` is the strict live window, unchanged — /api/remote/displays widened its
    listing for sleeping e-ink, this did not, so nothing that asks "is this display reachable?" moved."""
    row = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()
    if not row:
        return {"display_id": display_id, "active": False, "playlist": None, "artwork": None}
    return {**_display_now_playing(db, row), "active": _is_live(row, datetime.now(UTC))}

# /api/health/host + the appliance update bridge (/api/appliance/update*) now live in
# routers/health.py.


class RemoteChangeRequest(BaseModel):
    target_display: str
    action: str
    playlist: Optional[str] = None
    mode: Optional[str] = None


@router.post("/api/remote/change")
async def remote_change_playlist(request: RemoteChangeRequest, db: Session = Depends(get_db)):
    """Targeted command to change a playlist, mode, or trigger navigation on a specific display."""
    # H5r: display_id was the one place H5's validation didn't reach. Deliberately NOT
    # appliance_settings.validate("DISPLAY_ID", ...) — that's sd-conf's conf-writer rule
    # (lowercase [a-z0-9_-] only) for the one path that writes this into a shell-sourced conf file,
    # and it fails CLOSED (refuses everything) whenever sd-conf isn't importable. This path never
    # touches a shell or filesystem — it's logged/persisted/queried only — so it gets its own
    # runtime validator that admits the ids real displays already use.
    error = appliance_settings.validate_display_id_runtime(request.target_display)
    if error:
        raise HTTPException(400, detail=error)
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

@router.websocket("/ws/{display_id}")
async def websocket_endpoint(websocket: WebSocket, display_id: str):
    """Handles targeted display connections with multi-worker synchronization."""
    # H5: WebSockets are not covered by CORS, so a hostile page could otherwise open this socket
    # (CSWSH) to observe/redirect a display. Reject a cross-origin handshake; a browser always sends
    # Origin, while native kiosk/CDP clients send none (allowed — the accepted LAN-presence model).
    origin = websocket.headers.get("origin", "")
    if origin and not _origin_allowed(origin, websocket.headers.get("host", "")):
        await websocket.close(code=1008)
        return
    # H5r: display_id was still unvalidated here (H5 fixed the broadcast, not the path param) — same
    # runtime validator as /api/remote/change (see comment there for why not appliance_settings.validate),
    # closed with the WS policy-violation code (1008).
    if appliance_settings.validate_display_id_runtime(display_id):
        await websocket.close(code=1008)
        return
    await manager.connect(websocket, display_id)

    # Liveness is PAGE-OWNED (2026-09-20). The Canvas sends {"action":"heartbeat"} every 5s from its own
    # JS; only that frame bumps last_seen_at. The previous design had THIS handler write the heartbeat
    # every 5s for as long as the socket stayed open — and a socket stays open on protocol-level
    # ping/pong that the *browser process* answers with the page's main thread wedged. The prod Pi sat
    # a week with `active: true`, a healthy watchdog, and a frozen picture (memory: prod-kiosk-wedge).
    # A dead page now goes `active: false` in LIVE_WINDOW_SEC, which the host watchdog acts on.
    def _page_heartbeat():
        """Upsert last_seen_at for a heartbeat frame the page's JS actually sent."""
        try:
            with SessionLocal() as db:
                display = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()
                if display:
                    display.last_seen_at = datetime.now(UTC)
                else:
                    db.add(ActiveDisplayModel(display_id=display_id))
                db.commit()
        except Exception as e:
            logger.error(f"Heartbeat error for {display_id}: {e}", exc_info=True)

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

    # Start the command relay
    poller_task = asyncio.create_task(command_poller())

    try:
        while True:
            # A frame sent up this socket is echoed only to sockets on THIS display_id — never
            # broadcast to every screen (H5: that let one anonymous client inject to all displays).
            data = await websocket.receive_json()
            if isinstance(data, dict) and data.get("action") == "heartbeat":
                _page_heartbeat()          # liveness only — never echoed to the display's sockets
                continue
            await manager.send_personal_message(data, display_id)
    except WebSocketDisconnect:
        manager.disconnect(websocket, display_id)
    except Exception as e:
        logger.error(f"WebSocket error on '{display_id}': {e}", exc_info=True)
        manager.disconnect(websocket, display_id)
    finally:
        poller_task.cancel()
        # Clean up heartbeat from DB immediately on clean disconnect
        with SessionLocal() as db:
            db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).delete()
            db.commit()
