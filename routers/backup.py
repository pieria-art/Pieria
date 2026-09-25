"""Admin Backup & Restore API (ADR-138's reflash path). Build side in core/backup.py, restore side in
core/restore.py, boot-time apply in core/restore_boot.py — this router is just the HTTP surface over
those, plus the busy/idle bookkeeping shared between backup and restore (they can't run concurrently;
neither can run while the appliance update bridge is mid-action) and the auth gate every mutating
endpoint here shares with the appliance update bridge (a backup can carry API keys; a restore can queue
a host restart) — see `require_trusted_request` in routers/health.py.
"""
import json
import logging
import secrets as secrets_mod
import shutil
import threading
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

import config
import core.backup as backup
import core.restore as restore
from database import get_db
from models import SettingsModel
from routers.health import _busy_status, queue_appliance_action, require_trusted_request

logger = logging.getLogger("artwork-display-api.backup-router")

router = APIRouter()

MIN_PASSPHRASE_LEN = 12


def _refuse_if_busy() -> None:
    if backup.is_building():
        raise HTTPException(status_code=409, detail="a backup is already building")
    if restore.is_busy():
        raise HTTPException(status_code=409, detail="a restore is already in progress")
    if _busy_status():
        raise HTTPException(status_code=409, detail="the appliance update bridge is busy")


# --- Create backup -------------------------------------------------------------------------------

class BackupRequest(BaseModel):
    include_secrets: bool = False
    passphrase: Optional[str] = None


@router.post("/api/backup")
async def create_backup(req: BackupRequest, request: Request,
                        x_appliance_token: Optional[str] = Header(None)):
    require_trusted_request(request, x_appliance_token,
                            detail="creating a backup requires a same-origin request or a valid token")
    backup.sweep_expired()
    _refuse_if_busy()
    if req.include_secrets:
        if not req.passphrase or len(req.passphrase) < MIN_PASSPHRASE_LEN:
            raise HTTPException(status_code=400,
                                 detail=f"a passphrase of at least {MIN_PASSPHRASE_LEN} characters is "
                                        f"required to include API keys")
    estimated = backup._estimate_size_bytes()
    if not backup.free_space_ok(estimated, config.BACKUP_DIR):
        raise HTTPException(status_code=400, detail="not enough free space to build a backup safely")

    # The token is minted HERE, synchronously, and returned ONLY in this response — GET
    # /api/backup/status is unauthenticated (no origin/token gate; it's just a progress poll) and must
    # never be able to hand out a bearer credential for the finished archive. A caller that doesn't
    # capture this token (e.g. a page reload mid-build) has no way to retrieve it later; the frontend
    # tells the admin to start a fresh backup in that case rather than pretending it can recover one.
    token = secrets_mod.token_urlsafe(32)
    # Written synchronously, BEFORE the background thread even starts — otherwise a poller that hits
    # GET /api/backup/status in the window between this response and the thread's own first
    # _write_status call would still see whatever stale status (e.g. a previous "ready") was left
    # over, which a UI could momentarily render as if it still applied.
    backup._write_status(state="building", progress=0, message="starting")
    threading.Thread(
        target=backup.build_backup, args=(req.include_secrets, req.passphrase, token), daemon=True,
    ).start()
    return {"status": "building", "token": token}


@router.get("/api/backup/status")
async def backup_status():
    backup.sweep_expired()
    status = backup.read_status()
    # Belt-and-braces: even if something upstream ever put a token/url in here, strip it before it
    # reaches an unauthenticated poller. Today's build_backup never writes one, but this is the actual
    # security boundary — encoded here, not just "trust the writer".
    status.pop("token", None)
    status.pop("download_url", None)
    return status


@router.get("/api/backup/download/{token}")
async def download_backup(token: str):
    backup.sweep_expired()
    if not backup.TOKEN_RE.fullmatch(token):
        raise HTTPException(status_code=404, detail="not found")
    sidecar = config.BACKUP_DIR / f"{token}.json"
    tar_path = config.BACKUP_DIR / f"{token}.tar"
    if not sidecar.exists() or not tar_path.exists():
        raise HTTPException(status_code=404, detail="not found or already downloaded")
    meta = json.loads(sidecar.read_text())

    def _cleanup():
        sidecar.unlink(missing_ok=True)
        tar_path.unlink(missing_ok=True)
        backup._write_status(state="idle")

    return FileResponse(tar_path, media_type="application/x-tar", filename=meta["filename"],
                        background=BackgroundTask(_cleanup))


# --- Restore ---------------------------------------------------------------------------------------

@router.post("/api/restore/upload")
async def upload_restore(request: Request, x_appliance_token: Optional[str] = Header(None)):
    require_trusted_request(request, x_appliance_token,
                            detail="restoring requires a same-origin request or a valid token")
    backup.sweep_expired()
    _refuse_if_busy()

    content_length = request.headers.get("content-length")
    try:
        free = shutil.disk_usage(config.RESTORE_DIR if config.RESTORE_DIR.exists()
                                 else config.RESTORE_DIR.parent).free
    except OSError:
        free = None
    # Extraction needs ~2x the upload on disk at once — cap the upload at HALF of (free - 1GB headroom).
    cap = restore.upload_cap_bytes(free) if free is not None else None
    if cap is not None and content_length is not None:
        try:
            if int(content_length) > cap:
                raise HTTPException(status_code=413, detail="upload exceeds available free space")
        except ValueError:
            pass

    config.RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        config.RESTORE_DIR.chmod(0o700)
    except OSError:
        pass
    restore._write_status(state="uploading")
    total = 0
    try:
        try:
            with restore.UPLOAD_PATH.open("wb") as f:
                async for chunk in request.stream():
                    total += len(chunk)
                    if cap is not None and total > cap:
                        raise HTTPException(status_code=413, detail="upload exceeds available free space")
                    f.write(chunk)
            try:
                restore.UPLOAD_PATH.chmod(0o600)
            except OSError:
                pass
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — a truncated/disconnected upload must not wedge the feature
            logger.warning(f"[Restore] upload failed: {type(e).__name__}: {e}")
            raise HTTPException(status_code=400, detail="upload failed — try again")

        restore._write_status(state="validating")
        try:
            # sha256 over every file (possibly GBs) + full extraction — run off the event loop so it
            # can't block this worker from serving anything else while a big archive validates.
            summary = await run_in_threadpool(restore.validate_uploaded_archive)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — a corrupt DB/tar must produce a clean 400, not a stuck state
            logger.warning(f"[Restore] validation crashed: {type(e).__name__}: {e}", exc_info=True)
            raise HTTPException(status_code=400, detail="could not validate the archive — it may be "
                                                          "corrupt")
        return summary
    except HTTPException:
        restore.UPLOAD_PATH.unlink(missing_ok=True)
        restore._write_status(state="idle")
        raise
    except Exception as e:  # noqa: BLE001 — belt-and-braces: NOTHING leaves the status stuck
        logger.error(f"[Restore] upload/validate failed unexpectedly: {e}", exc_info=True)
        restore.UPLOAD_PATH.unlink(missing_ok=True)
        restore._write_status(state="idle")
        raise HTTPException(status_code=400, detail="upload failed — try again")


class RestoreConfirmRequest(BaseModel):
    passphrase: Optional[str] = None
    skip_secrets: bool = False


@router.post("/api/restore/confirm")
async def confirm_restore(req: RestoreConfirmRequest, request: Request,
                          x_appliance_token: Optional[str] = Header(None)):
    require_trusted_request(request, x_appliance_token,
                            detail="restoring requires a same-origin request or a valid token")
    _refuse_if_busy_for_confirm()
    # core.restore.confirm() owns its own state transitions (validated -> staging -> staged, or back to
    # validated/idle on a failure) — it requires state=="validated" going in and raises 409 otherwise.
    restore.confirm(req.passphrase, req.skip_secrets)

    if config.IS_APPLIANCE:
        busy = _busy_status()
        if busy:
            # Staged already succeeded — the restart just needs a nudge; the UI can retry the bridge
            # action separately (Devices → Restart app) if this specific queue call loses the race.
            logger.warning(f"[Restore] staged, but the appliance bridge was busy ({busy.get('action')}) "
                           f"— restart not auto-queued; the admin must restart the app manually")
            return {"staged": True, "restart_queued": False, "restart_required": False}
        queue_appliance_action("restart-app")
        return {"staged": True, "restart_queued": True, "restart_required": False}
    return {"staged": True, "restart_queued": False, "restart_required": True}


def _refuse_if_busy_for_confirm() -> None:
    # confirm() legitimately runs while restore's own status says "validated" (that's the expected
    # predecessor state) — only refuse on a genuinely concurrent backup build or appliance action.
    if backup.is_building():
        raise HTTPException(status_code=409, detail="a backup is already building")
    if _busy_status():
        raise HTTPException(status_code=409, detail="the appliance update bridge is busy")


@router.get("/api/restore/status")
async def restore_status(db: Session = Depends(get_db)):
    """Combined state for the UI: the upload/validate/stage/boot-apply status file, plus whatever's
    still pending from a DB point of view (pack re-download in progress, device conf not yet applied)."""
    status = restore.read_status()
    packs_row = db.query(SettingsModel).filter(SettingsModel.setting_key == "restore_pending_packs").first()
    conf_row = db.query(SettingsModel).filter(SettingsModel.setting_key == "restore_pending_conf").first()
    status["pending_packs"] = (json.loads(packs_row.setting_value)
                               if packs_row and packs_row.setting_value else None)
    status["pending_conf"] = (json.loads(conf_row.setting_value)
                              if conf_row and conf_row.setting_value else None)
    return status


@router.post("/api/restore/retry-packs")
async def retry_pending_packs(request: Request, x_appliance_token: Optional[str] = Header(None)):
    """Re-kicks core/lifespan._restore_pending_packs_loop for whatever pack cids are still listed under
    the `restore_pending_packs` setting after a restore — the "retry" button in the UI's pack
    re-download progress panel. Routed through the same cross-worker lock the leader-boot path uses
    (core.lifespan.is_redownloading_packs) — a second kick while one's already running is a 409, not a
    silent double-run."""
    require_trusted_request(request, x_appliance_token,
                            detail="this requires a same-origin request or a valid token")
    from core.lifespan import _restore_pending_packs_loop, _spawn, is_redownloading_packs
    if is_redownloading_packs():
        raise HTTPException(status_code=409, detail="pack re-download is already running")
    _spawn(_restore_pending_packs_loop())
    return {"status": "retrying"}


@router.post("/api/restore/pending-conf/clear")
async def clear_pending_conf(request: Request, x_appliance_token: Optional[str] = Header(None),
                             db: Session = Depends(get_db)):
    """The 'Apply device settings' button runs the EXISTING appliance bridge actions itself (no new
    root/bridge code — see the spec) and calls this once it's applied them all, so the prompt doesn't
    keep reappearing on every admin page load."""
    require_trusted_request(request, x_appliance_token,
                            detail="this requires a same-origin request or a valid token")
    db.query(SettingsModel).filter(SettingsModel.setting_key == "restore_pending_conf").delete()
    db.commit()
    return {"cleared": True}


@router.post("/api/restore/outcome/clear")
async def clear_restore_outcome(request: Request, x_appliance_token: Optional[str] = Header(None)):
    """Dismiss the boot-outcome banner (restored/restored_partial/failed) the admin card shows after a
    restart — see core/restore_boot.py's `_write_outcome` for what writes it."""
    require_trusted_request(request, x_appliance_token,
                            detail="this requires a same-origin request or a valid token")
    restore.clear_outcome()
    return {"cleared": True}
