"""Federation — subscribe to third-party Manifest v2 collections by URL.

The mirror of Publisher Studio (routers/publisher.py): that authors a feed, this consumes one.
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import federation
from config import SUB_PREFIX
from database import get_db
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
    }


class SubscriptionPayload(BaseModel):
    url: str


@router.get("/api/subscriptions")
async def list_subscriptions(db: Session = Depends(get_db)):
    """External (URL-added) collections only. `pack:` rows are installed local packs (ADR-044) — they're
    managed under Curated Art (owned tiles), and syncing one would try to HTTP-fetch a `pack:<id>` URL."""
    subs = db.query(SubscriptionModel).filter(
        SubscriptionModel.url.notlike("pack:%")).order_by(SubscriptionModel.id).all()
    return [_sub_summary(s) for s in subs]


@router.post("/api/subscriptions")
async def add_subscription(payload: SubscriptionPayload, db: Session = Depends(get_db)):
    """Subscribe to a publisher's Manifest v2 URL. Fetched + safety-checked + validated BEFORE a row
    is created, so a bad/unsafe URL never persists. Trust starts at 'community' (URL-added)."""
    url = payload.url.strip()
    if db.query(SubscriptionModel).filter(SubscriptionModel.url == url).first():
        raise HTTPException(409, detail="Already subscribed to this URL")
    try:
        manifest = await federation.fetch_manifest(url)
    except federation.FederationError as e:
        raise HTTPException(400, detail=str(e)) from e
    pub = manifest.get("publisher") or {}
    sub = SubscriptionModel(
        url=url, collection_id=manifest.get("id"), title=manifest.get("title"),
        publisher_id=pub.get("id"), publisher_name=pub.get("name"), publisher_url=pub.get("url"),
        trust=federation.assess_trust(manifest), enabled=True, cached_manifest=json.dumps(manifest),
        item_count=len(manifest.get("items", [])), last_status="ok", last_synced=datetime.now(UTC))
    db.add(sub); db.commit(); db.refresh(sub)
    return _sub_summary(sub)


@router.post("/api/subscriptions/{sub_id}/sync")
async def sync_subscription_endpoint(sub_id: int, db: Session = Depends(get_db)):
    sub = db.query(SubscriptionModel).filter(SubscriptionModel.id == sub_id).first()
    if not sub:
        raise HTTPException(404)
    await federation.sync_subscription(db, sub)
    return _sub_summary(sub)


@router.delete("/api/subscriptions/{sub_id}")
async def delete_subscription(sub_id: int, db: Session = Depends(get_db)):
    sub = db.query(SubscriptionModel).filter(SubscriptionModel.id == sub_id).first()
    if not sub:
        raise HTTPException(404)
    db.delete(sub); db.commit()
    return {"status": "removed"}
