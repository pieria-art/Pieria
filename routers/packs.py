"""Browse & download modular packs (ADR-040 #4 "browse & download packs" card).

Reads the public pack registry (packs.json — `tools/publish_pack` output on Cloudflare R2 behind
curwe.ai), offers each collection with an installed/available state, and installs a chosen one on demand
(background download + sha256 verify + append-install via `core.pack_fetch`). The device holds no secret —
the registry URL is public (ADR-038 §5). The baked Core is untouched; a pull only *appends*.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from config import PACK_REGISTRY_URL
from core import lifespan as lifespan_module
from core import pack_fetch
from database import SessionLocal, get_db
from models import SettingsModel, SubscriptionModel

router = APIRouter()
logger = logging.getLogger("artwork-display-api.packs")

# In-flight/finished install jobs by collection id -> {state: 'in_progress'|'done'|'error', trust?, error?}.
_JOBS: dict[str, dict] = {}
_FIELDS = ("id", "title", "category", "item_count", "bytes", "core", "cover")


def _registry_url(db: Session) -> str:
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == "pack_registry_url").first()
    return row.setting_value if row and row.setting_value else PACK_REGISTRY_URL


@router.get("/api/packs")
async def list_packs(db: Session = Depends(get_db)):
    """The registry annotated with per-collection install state (for the browse card). Degrades to an
    `error` field + empty list when the registry can't be reached, so the card shows a friendly message."""
    url = _registry_url(db)
    # cid -> trust for installed packs; every registry pack is Official (this is the signed screendocent registry).
    installed = {s.url.split("pack:", 1)[1]: s.trust
                 for s in db.query(SubscriptionModel).filter(SubscriptionModel.url.like("pack:%")).all()}
    client = pack_fetch.new_client()
    try:
        reg = await pack_fetch.fetch_registry(client, url)
    except Exception as e:  # noqa: BLE001 — surface an unreachable/invalid registry to the UI, don't 500
        return {"registry_url": url, "error": f"{type(e).__name__}: {e}", "core": [], "collections": []}
    finally:
        await client.aclose()

    cols = []
    for c in reg.get("collections", []):
        row = {k: c.get(k) for k in _FIELDS}
        cid = c.get("id")
        row["installed"] = cid in installed
        # Trust badge: an installed pack shows what the device verified (verified/community); an available
        # one shows Official (it's from the signed screendocent registry, verified for real at install).
        row["trust"] = installed.get(cid) or "official"
        row["job"] = _JOBS.get(cid, {}).get("state")
        cols.append(row)
    return {"registry_url": url, "core": reg.get("core", []), "collections": cols}


async def _install_job(collection_id: str, url: str) -> None:
    _JOBS[collection_id] = {"state": "in_progress"}
    db = SessionLocal()
    client = pack_fetch.new_client()
    try:
        res = await pack_fetch.install_collection_from_registry(db, client, url, collection_id)
        _JOBS[collection_id] = {"state": "done" if res.get("ok") else "error",
                                "trust": res.get("trust"), "error": res.get("error")}
        logger.info(f"[Packs] install {collection_id!r} -> {_JOBS[collection_id]}")
    except Exception as e:  # noqa: BLE001 — a job failure must never crash the app
        _JOBS[collection_id] = {"state": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        await client.aclose()
        db.close()


@router.post("/api/packs/{collection_id}/install")
async def install_pack(collection_id: str, db: Session = Depends(get_db)):
    """Kick off a background download+install of one collection; the card polls /api/packs/status."""
    if _JOBS.get(collection_id, {}).get("state") == "in_progress":
        return {"state": "in_progress"}
    asyncio.create_task(_install_job(collection_id, _registry_url(db)))
    return {"state": "started"}


@router.get("/api/packs/status")
async def packs_status():
    """In-flight/finished install jobs (collection_id -> {state, trust?, error?}) for the card to poll."""
    return _JOBS


@router.delete("/api/packs/{collection_id}")
async def uninstall_pack(collection_id: str, db: Session = Depends(get_db)):
    """Tier-2 'Remove collection': fully uninstall a downloaded pack — drop its subscription + playlist and
    reclaim the disk of any artworks left unlinked (shared masters + personal photos are kept). Distinct
    from Tier-1 `DELETE /playlists/{id}`, which keeps every work in the library. 404 if not installed."""
    res = lifespan_module.uninstall_collection(db, collection_id)
    if res is None:
        raise HTTPException(404, detail=f"collection {collection_id!r} is not installed")
    _JOBS.pop(collection_id, None)  # clear any stale install-job state so it re-shows as available
    return {"state": "uninstalled", **res}
