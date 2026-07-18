"""ADR-040 #4 — the "browse & download packs" endpoints (routers/packs.py): list the registry with
per-collection install state, kick off a background install, and expose job status for polling.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import app
from core import pack_fetch
from database import Base, get_db
from models import ArtworkModel, PlaylistModel, SubscriptionModel, playlist_artwork
from routers import packs as packs_router


@pytest.fixture
def db():
    # StaticPool shares one in-memory connection across threads (TestClient runs the endpoint off-thread).
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    packs_router._JOBS.clear()
    yield TestClient(app)
    app.dependency_overrides.clear()
    packs_router._JOBS.clear()


def _fake_registry():
    return {"core": ["masterpieces"], "collections": [
        {"id": "masterpieces", "title": "Masterpieces", "category": "featured", "item_count": 40, "bytes": 3, "core": True, "cover": "covers/masterpieces.jpg"},
        {"id": "cartography", "title": "Cartography", "category": "map", "item_count": 20, "bytes": 2, "core": False, "cover": "covers/cartography.jpg"},
        {"id": "cosmos", "title": "Cosmos", "category": "photo", "item_count": 15, "bytes": 1, "core": False, "cover": "covers/cosmos.jpg"},
    ]}


def test_list_packs_annotates_installed(client, db, monkeypatch):
    async def fake_fetch(_c, _u):
        return _fake_registry()
    monkeypatch.setattr(pack_fetch, "fetch_registry", fake_fetch)
    db.add(SubscriptionModel(url="pack:cartography", title="Cartography", trust="verified"))
    db.commit()

    d = client.get("/api/packs").json()
    assert d["core"] == ["masterpieces"]
    by = {c["id"]: c for c in d["collections"]}
    assert by["cartography"]["installed"] is True
    assert by["cosmos"]["installed"] is False
    assert by["masterpieces"]["core"] is True and by["cartography"]["category"] == "map"
    assert by["masterpieces"]["cover"] == "covers/masterpieces.jpg"  # cover passthrough for the browse grid
    assert by["cartography"]["trust"] == "verified"  # installed -> device-verified trust
    assert by["cosmos"]["trust"] == "official"       # available -> Official (from the signed registry)


def test_uninstall_endpoint_removes_subscription_and_playlist(client, db):
    import json
    # The Collection's manifest lists the work (own_urls) so uninstall reclaims it; the gallery links back.
    manifest = {"title": "Cartography", "items": [{"title": "Map", "image": {"local_file": "c.jpg"}}]}
    sub = SubscriptionModel(url="pack:cartography", title="Cartography", trust="verified",
                            cached_manifest=json.dumps(manifest))
    db.add(sub); db.commit(); db.refresh(sub)
    pl = PlaylistModel(name="Cartography", is_personal=False, source_subscription_id=sub.id)
    db.add(pl); db.commit(); db.refresh(pl)
    art = ArtworkModel(filename="c.jpg", status="approved", is_seed=True, title="Map",
                       source_url="pack:c.jpg")
    db.add(art); db.commit(); db.refresh(art)
    db.execute(playlist_artwork.insert().values(playlist_id=pl.id, artwork_id=art.id))
    db.commit()

    r = client.delete("/api/packs/cartography")
    assert r.status_code == 200
    assert r.json()["state"] == "uninstalled" and r.json()["artworks_removed"] == 1
    assert db.query(SubscriptionModel).filter(SubscriptionModel.url == "pack:cartography").first() is None
    assert db.query(PlaylistModel).filter(PlaylistModel.name == "Cartography").first() is None
    assert db.query(ArtworkModel).filter(ArtworkModel.title == "Map").first() is None


def test_uninstall_endpoint_404_when_not_installed(client):
    assert client.delete("/api/packs/nope").status_code == 404


def test_list_packs_registry_unreachable_degrades(client, monkeypatch):
    async def boom(_c, _u):
        raise RuntimeError("dns fail")
    monkeypatch.setattr(pack_fetch, "fetch_registry", boom)
    d = client.get("/api/packs").json()  # 200, not 500
    assert d["collections"] == [] and "dns fail" in d["error"]


def test_install_endpoint_starts_and_dedups(client, db, monkeypatch):
    async def fake_install(_db, _client, _url, _cid):
        return {"ok": True, "trust": "verified", "installed": True}
    monkeypatch.setattr(pack_fetch, "install_collection_from_registry", fake_install)
    monkeypatch.setattr(packs_router, "SessionLocal", lambda: db)

    assert client.post("/api/packs/cartography/install").json()["state"] == "started"
    # a second request while one is in flight is a no-op
    packs_router._JOBS["cosmos"] = {"state": "in_progress"}
    assert client.post("/api/packs/cosmos/install").json()["state"] == "in_progress"
    assert client.get("/api/packs/status").json()["cosmos"]["state"] == "in_progress"


@pytest.mark.asyncio
async def test_install_job_records_result(db, monkeypatch):
    async def fake_install(_db, _client, _url, _cid):
        return {"ok": True, "trust": "verified", "installed": True}
    monkeypatch.setattr(pack_fetch, "install_collection_from_registry", fake_install)
    monkeypatch.setattr(packs_router, "SessionLocal", lambda: db)
    packs_router._JOBS.clear()

    await packs_router._install_job("cartography", "https://packs.test/packs.json")
    assert packs_router._JOBS["cartography"]["state"] == "done"
    assert packs_router._JOBS["cartography"]["trust"] == "verified"


@pytest.mark.asyncio
async def test_install_job_records_error(db, monkeypatch):
    async def fake_install(_db, _client, _url, _cid):
        return {"ok": False, "installed": False, "error": "sha256 mismatch"}
    monkeypatch.setattr(pack_fetch, "install_collection_from_registry", fake_install)
    monkeypatch.setattr(packs_router, "SessionLocal", lambda: db)
    packs_router._JOBS.clear()

    await packs_router._install_job("cartography", "https://packs.test/packs.json")
    assert packs_router._JOBS["cartography"]["state"] == "error"
    assert "sha256" in packs_router._JOBS["cartography"]["error"]
