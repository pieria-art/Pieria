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
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import config
import host_health
from core import appliance_settings
from core.playback import _now_playing_artwork
from core.security import _origin_allowed
from database import get_db
from models import ActiveDisplayModel, DisplayPlaybackSessionModel, RemoteCommandModel

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
#: The whitelist. `sd-update` matches the same strings in a `case` and does the work; nothing here is
#: ever evaluated, and every value-bearing field is validated here AND again on the host (ADR-119).
ALLOWED_UPDATE_ACTIONS = {
    # existing (ADR-032 / ADR-071)
    "update-app", "update-scripts", "reboot",
    # device settings
    "set-timezone", "preview-orientation", "set-orientation", "set-display-name", "set-watchdog",
    "set-os-schedule", "reopen-setup",
    # actions
    "relaunch-kiosk", "restart-app", "poweroff", "support-bundle",
    # OS updates
    "check-os-updates", "update-system",
}
_appliance_token_warned = False

#: A queued/running action younger than this blocks a second one. Long enough to cover a full
#: `update-system` (apt + rebuild), short enough that a status stranded by a reboot mid-action
#: doesn't lock the only management surface this box has.
_BUSY_WINDOW = timedelta(minutes=30)


class ApplianceUpdateRequest(BaseModel):
    action: str
    ref: Optional[str] = None   # ADR-071: the release tag to check out (update-app); None = origin/main
    # Settings payloads. Each is validated against the SAME sd-conf validator the host will re-run;
    # only the fields ACTION_FIELDS declares for this action are ever copied into request.json.
    timezone: Optional[str] = None
    orientation: Optional[str] = None
    display_id: Optional[str] = None
    watchdog: Optional[str] = None
    schedule: Optional[str] = None
    time: Optional[str] = None


def _busy_status():
    """The in-flight action, if one is genuinely in flight. Returns None when idle, finished, or when
    the timestamp is missing/unparseable — a corrupt status file must not wedge the appliance."""
    status_file = config.APPLIANCE_DIR / "status.json"
    try:
        data = json.loads(status_file.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if data.get("state") not in ("queued", "running"):
        return None
    stamp = data.get("updated_at") or data.get("queued_at")
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return data if datetime.now(UTC) - when < _BUSY_WINDOW else None


def _collect_fields(req: ApplianceUpdateRequest) -> dict:
    """Validate and return ONLY the fields this action declares. Anything else the caller sent —
    including a valid-looking field belonging to a different action — is dropped, so `request.json`
    can never carry a key the host's arm didn't expect."""
    out = {}
    for field, conf_key, required in appliance_settings.ACTION_FIELDS.get(req.action, []):
        raw = getattr(req, field, None)
        raw = "" if raw is None else str(raw).strip()
        if not raw:
            if required:
                raise HTTPException(status_code=400, detail=f"{field} is required for {req.action}")
            continue
        if len(raw) > 64:
            raise HTTPException(status_code=400, detail=f"{field} is too long")
        value = raw
        if conf_key == "ROTATE":
            # The request carries the user-facing CHOICE (landscape|90|180|270); ROTATE is what that
            # choice means in the conf. Validate the conf value, transmit the choice — the host maps
            # it again through the same table and also derives EINK_ORIENTATION from it (ADR-059 #2).
            if raw not in appliance_settings.sd_conf.ORIENTATIONS:
                raise HTTPException(status_code=400, detail=f"invalid orientation: {raw}")
            value = appliance_settings.sd_conf.ORIENTATIONS[raw]
        elif conf_key == "DISPLAY_ID":
            # Sanitize FIRST and send the sanitized value: "Living Room!" is a reasonable thing to
            # type and must become living_room, not a 400.
            raw = value = appliance_settings.sanitize_display_id(raw)
        err = appliance_settings.validate(conf_key, value)
        if err:
            raise HTTPException(status_code=400, detail=err)
        out[field] = raw
    return out


def _migrate_display_id(db: Session, old_id: str, new_id: str) -> None:
    """Carry a renamed display's server-side state across. Best-effort by design: the rename has
    already been queued and WILL happen on the host, so a DB hiccup here must not 500 the request —
    it just means the new id starts with a fresh bag and an empty command queue.

    Playback sessions hold the shuffle bag (rename without this and the display replays works it just
    showed); queued remote commands would otherwise be delivered to a display id that no longer
    exists; and the old active_displays row would linger in the Devices console as a ghost until it
    aged out."""
    if not old_id or not new_id or old_id == new_id:
        return
    try:
        for model, column in ((DisplayPlaybackSessionModel, "display_id"),
                              (RemoteCommandModel, "target_display")):
            try:
                db.query(model).filter(getattr(model, column) == old_id).update(
                    {column: new_id}, synchronize_session=False)
                db.commit()
            except SQLAlchemyError:
                # A UNIQUE(display_id, playlist_id) clash means the new id already has a bag of its
                # own — keep it, drop the old rows rather than failing the rename.
                db.rollback()
        db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == old_id).delete(
            synchronize_session=False)
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        logger.warning(f"display rename {old_id} -> {new_id}: state migration skipped ({e})")


# A release tag we're willing to pass to the host updater. Deliberately strict — this string is handed
# to `git` on the host (as an argument, never eval'd, and re-validated there against the real tag list),
# but keeping the surface tiny here is the cheap first gate: semver-ish tags only.
_REF_RE = __import__("re").compile(r"^v?\d+(\.\d+){0,3}(-[0-9A-Za-z.]+)?$")


def require_trusted_request(request: Request, x_appliance_token: Optional[str],
                            detail: str = "this request requires a same-origin request or a valid token"
                            ) -> None:
    """Fail-closed origin-or-token gate. Shared by every endpoint that can trigger a host-consequential
    action OR return/consume a secret (the appliance update bridge, and Backup & Restore — a backup can
    carry API keys, a restore can queue restart-app): the cross-origin guard in app.py already blocks a
    hostile browser tab; the shared-secret token additionally closes the no-Origin path (curl / any
    other LAN device). Accept EITHER a valid X-Appliance-Token OR a trusted (same-origin) Origin, so the
    same-origin admin GUI keeps working without holding the secret. N6: fail CLOSED — with no token
    configured, a no-Origin caller (curl from any LAN device) must be refused outright, not waved
    through. Raises HTTPException(403) on refusal; returns None on success."""
    global _appliance_token_warned
    origin_ok = _origin_allowed(request.headers.get("origin", ""), request.headers.get("host", ""))
    token_ok = bool(config.APPLIANCE_UPDATE_TOKEN) and bool(x_appliance_token) and secrets.compare_digest(
        x_appliance_token, config.APPLIANCE_UPDATE_TOKEN)
    if not (token_ok or origin_ok):
        raise HTTPException(status_code=403, detail=detail)
    if not config.APPLIANCE_UPDATE_TOKEN and not _appliance_token_warned:
        logger.warning("SD_APPLIANCE_UPDATE_TOKEN is unset — this endpoint accepts same-origin "
                       "browser requests only; non-browser callers are refused.")
        _appliance_token_warned = True


@router.post("/api/appliance/update")
async def appliance_update(req: ApplianceUpdateRequest, request: Request,
                           x_appliance_token: Optional[str] = Header(None),
                           db: Session = Depends(get_db)):
    if not config.IS_APPLIANCE:
        raise HTTPException(status_code=403, detail="appliance update bridge not enabled")
    if req.action not in ALLOWED_UPDATE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action: {req.action}")
    # H6: this is the highest-consequence action (host git reset+rebuild / reboot).
    require_trusted_request(request, x_appliance_token,
                            detail="appliance update requires a same-origin request or a valid token")
    ref = (req.ref or "").strip()
    if ref and not _REF_RE.match(ref):
        raise HTTPException(status_code=400, detail=f"invalid release ref: {ref!r}")
    fields = _collect_fields(req)

    # One action at a time. Two concurrent `case` arms would race on the conf and the compose stack,
    # and the UI's preview -> keep flow is exactly the double-click this catches.
    busy = _busy_status()
    if busy:
        raise HTTPException(status_code=409,
                            detail=f"{busy.get('action', 'an action')} is already "
                                   f"{busy.get('state')} — wait for it to finish")

    nonce = queue_appliance_action(req.action, fields, ref)

    # The host renames the display; the server-side state keyed on the OLD id is ours to carry over.
    # Done here rather than on the host because only the app can reach the database.
    if req.action == "set-display-name":
        conf = host_health.read_conf() or {}
        _migrate_display_id(db, (conf.get("values") or {}).get("DISPLAY_ID", ""), fields["display_id"])

    logger.info(f"Appliance update queued: {req.action}{f' -> {ref}' if ref else ''} (nonce {nonce})")
    return {"status": "queued", "nonce": nonce}


def queue_appliance_action(action: str, fields: Optional[dict] = None, ref: Optional[str] = None) -> str:
    """Write request.json + status.json for the root `sd-update` watcher to pick up — the same queuing
    the GUI's /api/appliance/update uses, factored out so core/restore.py can queue `restart-app` after
    staging a restore without going through the HTTP request/origin-check machinery above (that
    machinery is for browser callers; an internal caller has already decided this action is warranted).
    Caller must have already checked `_busy_status()` — this never checks it itself."""
    nonce = secrets.token_hex(8)
    now = datetime.now(UTC).isoformat()
    config.APPLIANCE_DIR.mkdir(parents=True, exist_ok=True)
    # Write the status FIRST (so the .path trigger always finds a status), then the request.
    status = {"state": "queued", "action": action, "nonce": nonce,
              "message": "queued", "log_tail": [], "queued_at": now}
    (config.APPLIANCE_DIR / "status.json").write_text(json.dumps(status))
    payload = {"action": action, "requested_at": now, "nonce": nonce}
    if ref:
        payload["ref"] = ref
    payload.update(fields or {})
    (config.APPLIANCE_DIR / "request.json").write_text(json.dumps(payload))
    return nonce


@router.get("/api/appliance/support-bundle")
async def appliance_support_bundle():
    """Download the diagnostic tarball `sd-support-bundle` built into data/appliance/.

    The path is FIXED — no user input reaches it — so this cannot be walked into an arbitrary file
    read. The bundle itself is redacted at creation time (see sd-support-bundle's redact_conf)."""
    if not config.IS_APPLIANCE:
        raise HTTPException(status_code=403, detail="appliance update bridge not enabled")
    path = config.APPLIANCE_DIR / "support-bundle.tar.gz"
    if not path.exists():
        raise HTTPException(status_code=404, detail="no support bundle yet — create one first")
    return FileResponse(path, media_type="application/gzip", filename="pieria-support-bundle.tar.gz")


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
