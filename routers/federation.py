"""Federation — subscribe to third-party Manifest v2 collections by URL.

The mirror of Publisher Studio (routers/publisher.py): that authors a feed, this consumes one.
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

import federation
from config import SUB_PREFIX
from database import SessionLocal, get_db
from models import SubscriptionModel

router = APIRouter()


def _sub_summary(s: SubscriptionModel) -> dict:
    return {
        "id": s.id,
        "url": s.url,
        "collection_id": f"{SUB_PREFIX}{s.id}",
        "title": s.title,
        "publisher": {"id": s.publisher_id, "name": s.publisher_name, "url": s.publisher_url},
        "trust": s.trust,
        "enabled": s.enabled,
        "item_count": s.item_count,
        "last_synced": s.last_synced.isoformat() if s.last_synced else None,
        "last_status": s.last_status,
        "key_status": s.key_status or "ok",
        "pinned_fingerprint": federation.key_fingerprint(s.pinned_public_key),
        "pending_fingerprint": federation.key_fingerprint(s.pending_public_key),
    }


class SubscriptionPayload(BaseModel):
    url: str


@router.get("/api/subscriptions")
def list_subscriptions(db: Session = Depends(get_db)):
    """External (URL-added) collections only. `pack:` rows are installed local packs (ADR-044) — they're
    managed under Curated Art (owned tiles), and syncing one would try to HTTP-fetch a `pack:<id>` URL."""
    subs = db.query(SubscriptionModel).filter(
        SubscriptionModel.url.notlike("pack:%")).order_by(SubscriptionModel.id).all()
    return [_sub_summary(s) for s in subs]


# The three routes below `await` a manifest fetch, so they stay `async def` and do their DB work in
# threadpool helpers with their OWN short-lived session (ADR-148 / M6) — never a request-scoped one, and
# never held across the network await.

def _url_already_subscribed(url: str) -> bool:
    with SessionLocal() as db:
        return db.query(SubscriptionModel).filter(SubscriptionModel.url == url).first() is not None


def _create_subscription(url: str, manifest: dict) -> dict:
    pub = manifest.get("publisher") or {}
    with SessionLocal() as db:
        sub = SubscriptionModel(
            url=url, collection_id=manifest.get("id"), title=manifest.get("title"),
            publisher_id=pub.get("id"), publisher_name=pub.get("name"), publisher_url=pub.get("url"),
            trust=federation.assess_trust(manifest), pinned_public_key=federation.manifest_key(manifest),
            key_status="ok", enabled=True, cached_manifest=json.dumps(manifest),
            item_count=len(manifest.get("items", [])), last_status="ok", last_synced=datetime.now(UTC))
        db.add(sub); db.commit(); db.refresh(sub)
        return _sub_summary(sub)


def _subscription_url(sub_id: int) -> str:
    with SessionLocal() as db:
        sub = db.query(SubscriptionModel).filter(SubscriptionModel.id == sub_id).first()
        if not sub:
            raise HTTPException(404)
        return sub.url


def _apply_sync(sub_id: int, manifest: dict | None, error: Exception | None, retrust: bool = False) -> dict:
    with SessionLocal() as db:
        sub = db.query(SubscriptionModel).filter(SubscriptionModel.id == sub_id).first()
        if not sub:
            raise HTTPException(404)
        if error is not None:
            federation.record_sync_error(db, sub, error)
        else:
            federation.apply_synced_manifest(db, sub, manifest, retrust)
        return _sub_summary(sub)


async def _fetch_and_apply(sub_id: int, url: str, retrust: bool = False) -> dict:
    try:
        manifest = await federation.fetch_manifest(url)
    except federation.FederationError as e:
        return await run_in_threadpool(_apply_sync, sub_id, None, e)
    return await run_in_threadpool(_apply_sync, sub_id, manifest, None, retrust)


@router.post("/api/subscriptions")
async def add_subscription(payload: SubscriptionPayload):
    """Subscribe to a publisher's Manifest v2 URL. Fetched + safety-checked + validated BEFORE a row
    is created, so a bad/unsafe URL never persists. Trust starts at 'community' (URL-added)."""
    url = payload.url.strip()
    if await run_in_threadpool(_url_already_subscribed, url):
        raise HTTPException(409, detail="Already subscribed to this URL")
    try:
        manifest = await federation.fetch_manifest(url)
    except federation.FederationError as e:
        raise HTTPException(400, detail=str(e)) from e
    return await run_in_threadpool(_create_subscription, url, manifest)


@router.post("/api/subscriptions/{sub_id}/sync")
async def sync_subscription_endpoint(sub_id: int):
    url = await run_in_threadpool(_subscription_url, sub_id)
    return await _fetch_and_apply(sub_id, url)


def _retrust_prepare(sub_id: int) -> tuple[str | None, dict | None]:
    """Returns (url_to_resync, None) for a 'changed' row, or (None, summary) once a 'rotated' ack is done."""
    with SessionLocal() as db:
        sub = db.query(SubscriptionModel).filter(SubscriptionModel.id == sub_id).first()
        if not sub:
            raise HTTPException(404)
        if sub.key_status == "rotated":
            sub.key_status, sub.pending_public_key = "ok", None
            db.commit()
            return None, _sub_summary(sub)
        if sub.key_status == "changed":
            return sub.url, None
        raise HTTPException(400, detail="Nothing to re-trust")


@router.post("/api/subscriptions/{sub_id}/retrust")
async def retrust_subscription(sub_id: int):
    """Accept a publisher's changed key (pending -> pinned) and re-sync; or acknowledge a followed
    registry rotation. Only valid when the row is flagged."""
    url, summary = await run_in_threadpool(_retrust_prepare, sub_id)
    if summary is not None:
        return summary
    return await _fetch_and_apply(sub_id, url, retrust=True)


@router.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: int, db: Session = Depends(get_db)):
    sub = db.query(SubscriptionModel).filter(SubscriptionModel.id == sub_id).first()
    if not sub:
        raise HTTPException(404)
    db.delete(sub); db.commit()
    return {"status": "removed"}
