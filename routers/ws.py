"""WebSocket + remote control — extracted from app.py (Phase 3 of the app-split refactor).

The targeted WebSocket registry (core.connections.manager) is shared with the remote-command push
path here: `/ws/{display_id}` accepts a display's socket, and `POST /api/remote/change` persists a
command the socket's own `command_poller` picks up and relays — bridging across Uvicorn's multiple
worker processes (ADR-006), since no single worker sees every display's socket.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.connections import manager
from core.playback import _display_now_playing
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
    """Active displays (seen in the last 15s), each with what it's currently showing so the Remote can
    render a 'now showing' panel + highlight the active collection. Shape: {display_id, playlist, artwork}."""
    cutoff = datetime.now(UTC) - timedelta(seconds=15)
    displays = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.last_seen_at > cutoff).all()
    return [_display_now_playing(db, d) for d in displays]


@router.get("/api/displays/{display_id}/now-playing")
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


class RemoteChangeRequest(BaseModel):
    target_display: str
    action: str
    playlist: Optional[str] = None
    mode: Optional[str] = None


@router.post("/api/remote/change")
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
