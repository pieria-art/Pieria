"""Publisher Studio — author + sign a Manifest v2 feed of your OWN hosted images.

The mirror of Subscriptions (routers/federation.py): that consumes feeds, this AUTHORS one. Images
are URL-first (we never host them); the artist's Ed25519 identity key lives in SettingsModel and
signs server-side on export; the browser never sees the private key.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

import federation
import publisher
from core.settings_util import _upsert_setting
from database import get_db
from models import PublisherCollectionModel, SettingsModel

router = APIRouter()

_PUB_IDENTITY_KEYS = ("publisher_id", "publisher_name", "publisher_url",
                      "publisher_public_key", "publisher_private_key")


def _publisher_identity(db: Session) -> dict:
    rows = {s.setting_key: s.setting_value for s in
            db.query(SettingsModel).filter(SettingsModel.setting_key.in_(_PUB_IDENTITY_KEYS)).all()}
    return rows


def _identity_public(rows: dict) -> dict:
    """Public-safe view of the identity — NEVER includes the private key."""
    return {
        "id": rows.get("publisher_id") or "",
        "name": rows.get("publisher_name") or "",
        "url": rows.get("publisher_url") or "",
        "public_key": rows.get("publisher_public_key") or "",
        "has_private_key": bool(rows.get("publisher_private_key")),
    }


async def _assert_public_urls(items: list) -> None:
    """SSRF-guard every image URL the publisher pasted (defense in depth: the subscriber checks too,
    but we never persist a private/loopback target). C2: getaddrinfo is blocking → thread each check."""
    for it in items or []:
        for url in (it.get("full_url"), it.get("thumbnail_url")):
            if not url:
                continue
            try:
                await asyncio.to_thread(federation._assert_public_url, url)
            except federation.FederationError as e:
                raise HTTPException(400, detail=f"Image URL rejected ({url}): {e}") from e


def _collection_summary(c: PublisherCollectionModel) -> dict:
    try:
        items = json.loads(c.items_json or "[]")
    except ValueError:
        items = []
    return {"id": c.id, "slug": c.slug, "title": c.title, "item_count": len(items),
            "updated_at": c.updated_at.isoformat() if c.updated_at else None}


def _collection_detail(c: PublisherCollectionModel) -> dict:
    try:
        items = json.loads(c.items_json or "[]")
    except ValueError:
        items = []
    return {"id": c.id, "slug": c.slug, "title": c.title, "description": c.description,
            "default_license": c.default_license, "cover_image": c.cover_image, "items": items,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None}


def _unique_slug(db: Session, desired: str, exclude_id: int | None = None) -> str:
    base = publisher._slugify(desired)
    slug, n = base, 2
    while True:
        q = db.query(PublisherCollectionModel).filter(PublisherCollectionModel.slug == slug)
        if exclude_id is not None:
            q = q.filter(PublisherCollectionModel.id != exclude_id)
        if not q.first():
            return slug
        slug, n = f"{base}-{n}", n + 1


def _meta_for(c: PublisherCollectionModel, identity: dict) -> dict:
    return {"slug": c.slug, "title": c.title, "description": c.description,
            "default_license": c.default_license, "cover_image": c.cover_image,
            "publisher": {"id": identity.get("publisher_id"), "name": identity.get("publisher_name"),
                          "url": identity.get("publisher_url")}}


class PublisherIdentityPayload(BaseModel):
    id: str
    name: str
    url: Optional[str] = None
    regenerate: bool = False


class PublisherItemPayload(BaseModel):
    id: Optional[str] = None
    title: str
    artist: Optional[str] = None
    artist_role: Optional[str] = None
    date: Optional[str] = None
    creation_date: Optional[str] = None
    medium: Optional[str] = None
    culture: Optional[str] = None
    tags: Optional[List[str]] = None
    placard: Optional[str] = None
    full_url: str
    thumbnail_url: Optional[str] = None
    license: Optional[str] = None
    attribution: Optional[str] = None
    rights_holder: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    focal_point: Optional[List[float]] = None


class PublisherCollectionPayload(BaseModel):
    slug: Optional[str] = None
    title: str
    description: Optional[str] = None
    default_license: Optional[str] = None
    cover_image: Optional[str] = None
    items: List[PublisherItemPayload] = []


@router.get("/api/publisher/identity")
async def get_publisher_identity(db: Session = Depends(get_db)):
    return _identity_public(_publisher_identity(db))


@router.post("/api/publisher/identity")
async def set_publisher_identity(payload: PublisherIdentityPayload, db: Session = Depends(get_db)):
    """Save the publisher id/name/url and ensure an Ed25519 identity key exists. Generates a keypair on
    first save; `regenerate=true` rotates it — which invalidates the signature on anything already
    published (the response carries a warning the UI surfaces)."""
    pid = payload.id.strip()
    name = payload.name.strip()
    if not pid or not name:
        raise HTTPException(400, detail="Publisher id and name are required.")
    rows = _publisher_identity(db)
    _upsert_setting(db, "publisher_id", pid)
    _upsert_setting(db, "publisher_name", name)
    _upsert_setting(db, "publisher_url", (payload.url or "").strip())
    warning = None
    if payload.regenerate or not rows.get("publisher_private_key"):
        priv, pub = publisher.keygen()
        _upsert_setting(db, "publisher_private_key", priv)
        _upsert_setting(db, "publisher_public_key", pub)
        if payload.regenerate and rows.get("publisher_private_key"):
            warning = ("Signing key rotated. Any manifest you already published is now signed with the "
                       "old key — re-export and re-host it, and update the registry if you were verified.")
    db.commit()
    result = _identity_public(_publisher_identity(db))
    if warning:
        result["warning"] = warning
    return result


@router.get("/api/publisher/collections")
async def list_publisher_collections(db: Session = Depends(get_db)):
    return [_collection_summary(c) for c in
            db.query(PublisherCollectionModel).order_by(PublisherCollectionModel.id).all()]


async def _checked_cover(payload: PublisherCollectionPayload) -> str | None:
    cover = (payload.cover_image or "").strip() or None
    if cover:
        await _assert_public_urls([{"full_url": cover}])
    return cover


@router.post("/api/publisher/collections")
async def create_publisher_collection(payload: PublisherCollectionPayload, db: Session = Depends(get_db)):
    items = [it.model_dump() for it in payload.items]
    await _assert_public_urls(items)
    cover = await _checked_cover(payload)
    slug = _unique_slug(db, payload.slug or payload.title)
    norm = [publisher.build_item(it) for it in items]
    c = PublisherCollectionModel(
        slug=slug, title=payload.title.strip(), description=(payload.description or "").strip() or None,
        default_license=(payload.default_license or "").strip() or None, cover_image=cover,
        items_json=json.dumps(norm), created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    db.add(c); db.commit(); db.refresh(c)
    return _collection_detail(c)


def _get_collection(db: Session, cid: int) -> PublisherCollectionModel:
    c = db.query(PublisherCollectionModel).filter(PublisherCollectionModel.id == cid).first()
    if not c:
        raise HTTPException(404, detail="Collection not found")
    return c


@router.get("/api/publisher/collections/{cid}")
async def get_publisher_collection(cid: int, db: Session = Depends(get_db)):
    return _collection_detail(_get_collection(db, cid))


@router.put("/api/publisher/collections/{cid}")
async def update_publisher_collection(cid: int, payload: PublisherCollectionPayload,
                                      db: Session = Depends(get_db)):
    c = _get_collection(db, cid)
    items = [it.model_dump() for it in payload.items]
    await _assert_public_urls(items)
    cover = await _checked_cover(payload)
    if payload.slug and publisher._slugify(payload.slug) != c.slug:
        c.slug = _unique_slug(db, payload.slug, exclude_id=c.id)
    c.title = payload.title.strip()
    c.description = (payload.description or "").strip() or None
    c.default_license = (payload.default_license or "").strip() or None
    c.cover_image = cover
    c.items_json = json.dumps([publisher.build_item(it) for it in items])
    c.updated_at = datetime.now(UTC)
    db.commit(); db.refresh(c)
    return _collection_detail(c)


@router.delete("/api/publisher/collections/{cid}")
async def delete_publisher_collection(cid: int, db: Session = Depends(get_db)):
    db.delete(_get_collection(db, cid)); db.commit()
    return {"status": "removed"}


@router.post("/api/publisher/collections/{cid}/validate")
async def validate_publisher_collection(cid: int, db: Session = Depends(get_db)):
    c = _get_collection(db, cid)
    items = json.loads(c.items_json or "[]")
    _, errors = publisher.assemble_and_validate(_meta_for(c, _publisher_identity(db)), items)
    return {"valid": not errors, "errors": errors}


@router.post("/api/publisher/collections/{cid}/export")
async def export_publisher_collection(cid: int, db: Session = Depends(get_db)):
    """Assemble → validate → sign → download. 400 if no identity/key; 422 if the manifest is invalid."""
    c = _get_collection(db, cid)
    identity = _publisher_identity(db)
    if not identity.get("publisher_private_key"):
        raise HTTPException(400, detail="Set up your publisher identity first (it creates a signing key).")
    items = json.loads(c.items_json or "[]")
    await _assert_public_urls(items)
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    manifest, errors = publisher.assemble_validate_sign(
        _meta_for(c, identity), items,
        identity["publisher_private_key"], identity.get("publisher_public_key"),
        generated_at=generated_at)
    if errors:
        raise HTTPException(422, detail=errors)
    return Response(
        content=json.dumps(manifest, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{c.slug}.json"'})
