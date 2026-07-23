"""Device Health console + the appliance update bridge (all-in-one only).

The container is unprivileged and cannot run git/docker/reboot. So a GUI action just writes a
request file into the ./data bind mount; a root systemd .path unit notices it and runs the
whitelisted host helper `sd-update`, which writes status back here for the UI to poll. The web
app never gains host privileges.
"""

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

import config
import host_health
from core.playback import _now_playing_artwork
from core.security import _origin_allowed
from database import get_db
from models import ActiveDisplayModel

logger = logging.getLogger("artwork-display-api")

router = APIRouter()


@router.get("/api/health/host")
async def get_host_health(db: Session = Depends(get_db)):
    """Device Health console data: this box's host metrics + the displays it currently serves.

    All-in-one only — returns 404 on a generic/MS-01 server or thin-client topology (where the
    server isn't running ON the managed device), so the admin UI keeps the Devices tab hidden there.
    Compute-on-request: the readers are microseconds of /proc + /sys reads, so no DB table or
    background collector is needed."""
    if not config.IS_APPLIANCE:
        raise HTTPException(status_code=404, detail="host metrics unavailable")
    cutoff = datetime.now(UTC) - timedelta(seconds=15)
    displays = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.last_seen_at > cutoff).all()
    return {
        "available": True,
        "host": host_health.collect(),
        "displays": [
            {"display_id": d.display_id, "last_seen_at": d.last_seen_at.isoformat(),
             "playlist": d.current_playlist, "artwork": _now_playing_artwork(db, d.current_artwork_id)}
            for d in displays
        ],
    }

# --- Appliance update bridge (all-in-one only) -------------------------------------------------
# The container is unprivileged and cannot run git/docker/reboot. So a GUI action just writes a
# request file into the ./data bind mount; a root systemd .path unit notices it and runs the
# whitelisted host helper `sd-update`, which writes status back here for the UI to poll. The web
# app never gains host privileges.
ALLOWED_UPDATE_ACTIONS = {"update-app", "update-scripts", "reboot"}
_appliance_token_warned = False


class ApplianceUpdateRequest(BaseModel):
    action: str
    ref: Optional[str] = None   # ADR-071: the release tag to check out (update-app); None = origin/main


# A release tag we're willing to pass to the host updater. Deliberately strict — this string is handed
# to `git` on the host (as an argument, never eval'd, and re-validated there against the real tag list),
# but keeping the surface tiny here is the cheap first gate: semver-ish tags only.
_REF_RE = __import__("re").compile(r"^v?\d+(\.\d+){0,3}(-[0-9A-Za-z.]+)?$")


@router.post("/api/appliance/update")
async def appliance_update(req: ApplianceUpdateRequest, request: Request,
                           x_appliance_token: Optional[str] = Header(None)):
    global _appliance_token_warned
    if not config.IS_APPLIANCE:
        raise HTTPException(status_code=403, detail="appliance update bridge not enabled")
    if req.action not in ALLOWED_UPDATE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action: {req.action}")
    # H6: this is the highest-consequence action (host git reset+rebuild / reboot). The cross-origin
    # guard already blocks a hostile browser tab; the shared-secret token additionally closes the
    # no-Origin path (curl / any other LAN device). Accept EITHER a valid token OR a trusted
    # (same-origin) Origin, so the same-origin admin GUI keeps working without holding the secret.
    if config.APPLIANCE_UPDATE_TOKEN:
        token_ok = bool(x_appliance_token) and secrets.compare_digest(
            x_appliance_token, config.APPLIANCE_UPDATE_TOKEN)
        origin_ok = _origin_allowed(request.headers.get("origin", ""), request.headers.get("host", ""))
        if not (token_ok or origin_ok):
            raise HTTPException(status_code=403, detail="appliance update requires a valid token")
    elif not _appliance_token_warned:
        logger.warning("SD_APPLIANCE_UPDATE_TOKEN is unset — /api/appliance/update is gated only by the "
                       "cross-origin guard. Set it to require a shared secret from non-browser callers.")
        _appliance_token_warned = True
    ref = (req.ref or "").strip()
    if ref and not _REF_RE.match(ref):
        raise HTTPException(status_code=400, detail=f"invalid release ref: {ref!r}")
    nonce = secrets.token_hex(8)
    config.APPLIANCE_DIR.mkdir(parents=True, exist_ok=True)
    # Write the status FIRST (so the .path trigger always finds a status), then the request.
    status = {"state": "queued", "action": req.action, "nonce": nonce,
              "message": "queued", "log_tail": []}
    (config.APPLIANCE_DIR / "status.json").write_text(json.dumps(status))
    request = {"action": req.action, "requested_at": datetime.now(UTC).isoformat(), "nonce": nonce}
    if ref:
        request["ref"] = ref
    (config.APPLIANCE_DIR / "request.json").write_text(json.dumps(request))
    logger.info(f"Appliance update queued: {req.action}{f' -> {ref}' if ref else ''} (nonce {nonce})")
    return {"status": "queued", "nonce": nonce}


@router.get("/api/appliance/update/check")
async def appliance_update_check(refresh: bool = False):
    """Is a newer release available? Reads the cached result unless ?refresh=true (rate-limited to once
    per 15 min). Appliance-only, like the rest of the bridge. Never errors on a failed check — it
    reports {error: ...} so the admin UI can say 'couldn't check' instead of breaking."""
    if not config.IS_APPLIANCE:
        raise HTTPException(status_code=403, detail="appliance update bridge not enabled")
    from core import update_check
    return await update_check.check_for_update(force=refresh)


@router.get("/api/appliance/update/status")
async def appliance_update_status():
    if not config.IS_APPLIANCE:
        raise HTTPException(status_code=403, detail="appliance update bridge not enabled")
    status_file = config.APPLIANCE_DIR / "status.json"
    if not status_file.exists():
        return {"state": "idle"}
    try:
        return json.loads(status_file.read_text())
    except (OSError, json.JSONDecodeError):
        return {"state": "idle"}
