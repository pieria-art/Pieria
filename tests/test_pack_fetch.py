"""ADR-040 #4 modular packs — the runtime consumer: core.pack_fetch downloads a collection artifact from
the registry, verifies its sha256, extracts it into ARTWORK_ROOT, and append-installs it. Driven end-to-end
over an httpx MockTransport serving a real sliced pack (no network).
"""
import json
import shutil
import tarfile
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import federation
import publisher
from core import lifespan as lifespan_module
from core import pack_fetch
from database import Base
from models import ArtworkModel, SubscriptionModel
from tools import build_pack, publish_pack


def _mi(title, rank):
    return {"filename": f"{title.lower()}.jpg", "thumbnail": f"{title.lower()}_t.jpg",
            "source_url": f"https://x/{title}.jpg", "title": title, "agent_name": "A. Painter",
            "agent_role": "Artist", "cultural_context": "French", "description_narrative": "A placard.",
            "kind": "painting", "license": "Public Domain", "date_display": "1890",
            "focal_point": [0.5, 0.5], "featured_rank": rank, "credit_line": "Some Museum", "source": "Some Museum"}


def _build_source_pack(tmp_path, priv):
    root = tmp_path / "art-pack"
    lib = root / "_Library"
    thumbs = root / "_catalog_thumbs"
    lib.mkdir(parents=True)
    thumbs.mkdir(parents=True)
    cols = [
        {"id": "masterpieces", "title": "Masterpieces", "description": "Best", "default": True,
         "items": [_mi("Mona-Lisa", 99), _mi("Starry", 98)]},
        {"id": "cartography", "title": "Cartography", "description": "Maps", "items": [_mi("Map", 60)]},
    ]
    build_pack._emit_v2_manifests(root, cols, signing_key=priv, generated_at="2026-07-17")
    for cid in ("masterpieces", "cartography"):
        manifest = json.loads((root / "_manifests" / f"{cid}.json").read_text())
        for item in manifest["items"]:
            img = item["image"]
            Image.new("RGB", (30, 20), "red").save(lib / img["local_file"], "JPEG")
            tn = publish_pack._thumb_name(img.get("thumbnail_url"))
            if tn:
                Image.new("RGB", (12, 8), "blue").save(thumbs / tn, "JPEG")
    return root


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def _point_lifespan_at(monkeypatch, pub, root: Path):
    monkeypatch.setattr(lifespan_module, "ARTWORK_ROOT", root)
    monkeypatch.setattr(lifespan_module, "LIBRARY_DIR", root / "_Library")
    monkeypatch.setattr(lifespan_module, "PACK_INDEX", root / "pack-index.json")
    monkeypatch.setattr(federation, "TRUSTED_KEYS", {"pieria": pub})
    monkeypatch.setattr(pack_fetch.federation, "_assert_public_url", lambda url: None)  # test hosts aren't public


def _extract_into(tar_path: Path, dest_root: Path):
    tmp = dest_root.parent / ("_x_" + tar_path.stem)
    if tmp.exists():
        shutil.rmtree(tmp)
    with tarfile.open(tar_path) as tf:
        tf.extractall(tmp, filter="data")
    inner = tmp / tar_path.stem
    (dest_root / "_Library").mkdir(parents=True, exist_ok=True)
    (dest_root / "_manifests").mkdir(parents=True, exist_ok=True)
    for f in (inner / "_Library").iterdir():
        f.rename(dest_root / "_Library" / f.name)
    for f in (inner / "_manifests").iterdir():
        f.rename(dest_root / "_manifests" / f.name)


def _serve(dist):
    """MockTransport serving packs.json + the collection tars from a published dist/ dir."""
    def handler(request: httpx.Request) -> httpx.Response:
        p = dist / request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=p.read_bytes()) if p.exists() else httpx.Response(404)
    return httpx.MockTransport(handler)


def _bake_core(tmp_path, dist):
    device = tmp_path / "device"
    _extract_into(dist / "masterpieces.tar", device)
    (device / "pack-index.json").write_text(json.dumps({
        "pack_version": "2", "publisher": {"id": "pieria"},
        "collections": [{"id": "masterpieces", "title": "Masterpieces",
                         "manifest": "_manifests/masterpieces.json", "item_count": 2, "default": True}],
    }))
    return device


@pytest.mark.asyncio
async def test_fetch_installs_collection_from_registry(tmp_path, monkeypatch):
    priv, pub = publisher.keygen()
    src = _build_source_pack(tmp_path, priv)
    dist = tmp_path / "dist"
    publish_pack.publish(src, dist, core={"masterpieces"})
    device = _bake_core(tmp_path, dist)
    _point_lifespan_at(monkeypatch, pub, device)

    db = _db()
    lifespan_module.install_pack_subscriptions(db)
    assert db.query(SubscriptionModel).count() == 1

    client = httpx.AsyncClient(transport=_serve(dist))
    res = await pack_fetch.install_collection_from_registry(
        db, client, "https://packs.test/packs.json", "cartography")
    await client.aclose()

    assert res["ok"] and res["installed"] and res["trust"] == "verified", res
    assert {s.url for s in db.query(SubscriptionModel).all()} == {"pack:masterpieces", "pack:cartography"}
    assert db.query(ArtworkModel).count() == 3
    assert any((device / "_Library").iterdir())


@pytest.mark.asyncio
async def test_fetch_rejects_sha256_mismatch(tmp_path, monkeypatch):
    priv, pub = publisher.keygen()
    src = _build_source_pack(tmp_path, priv)
    dist = tmp_path / "dist"
    publish_pack.publish(src, dist, core={"masterpieces"})
    packs = json.loads((dist / "packs.json").read_text())
    for c in packs["collections"]:
        if c["id"] == "cartography":
            c["sha256"] = "0" * 64
    (dist / "packs.json").write_text(json.dumps(packs))

    device = tmp_path / "device"
    (device / "_manifests").mkdir(parents=True)
    _point_lifespan_at(monkeypatch, pub, device)

    db = _db()
    client = httpx.AsyncClient(transport=_serve(dist))
    res = await pack_fetch.install_collection_from_registry(
        db, client, "https://packs.test/packs.json", "cartography")
    await client.aclose()

    assert not res["ok"] and "sha256" in (res.get("error") or ""), res
    assert db.query(SubscriptionModel).count() == 0


@pytest.mark.asyncio
async def test_fetch_retries_rate_limited_download(tmp_path, monkeypatch):
    """A Cloudflare 429 on the .tar (then a Retry-After) is backed off and retried, not fatal (ADR-038)."""
    priv, pub = publisher.keygen()
    src = _build_source_pack(tmp_path, priv)
    dist = tmp_path / "dist"
    publish_pack.publish(src, dist, core={"masterpieces"})
    device = _bake_core(tmp_path, dist)
    _point_lifespan_at(monkeypatch, pub, device)

    slept = []
    monkeypatch.setattr(pack_fetch.asyncio, "sleep", lambda s: slept.append(s) or _noop())

    # 429 (with Retry-After) on the first tar request, then serve the real bytes.
    hits = {"cartography.tar": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "cartography.tar":
            hits[name] += 1
            if hits[name] == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
        p = dist / name
        return httpx.Response(200, content=p.read_bytes()) if p.exists() else httpx.Response(404)

    db = _db()
    lifespan_module.install_pack_subscriptions(db)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    res = await pack_fetch.install_collection_from_registry(
        db, client, "https://packs.test/packs.json", "cartography")
    await client.aclose()

    assert res["ok"] and res["installed"], res
    assert hits["cartography.tar"] == 2          # retried once after the 429
    assert slept == [2.0]                         # honored the Retry-After header
    assert db.query(ArtworkModel).count() == 3


async def _noop():
    return None


@pytest.mark.asyncio
async def test_fetch_unknown_collection(tmp_path, monkeypatch):
    priv, pub = publisher.keygen()
    src = _build_source_pack(tmp_path, priv)
    dist = tmp_path / "dist"
    publish_pack.publish(src, dist, core={"masterpieces"})
    device = tmp_path / "device"
    (device / "_manifests").mkdir(parents=True)
    _point_lifespan_at(monkeypatch, pub, device)

    db = _db()
    client = httpx.AsyncClient(transport=_serve(dist))
    res = await pack_fetch.install_collection_from_registry(
        db, client, "https://packs.test/packs.json", "nonexistent")
    await client.aclose()
    assert not res["ok"] and "registry" in (res.get("error") or "")
