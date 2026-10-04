"""ADR-040 #4 — the "browse & download packs" endpoints (routers/packs.py): list the registry with
per-collection install state, kick off a background install, and expose job status for polling.
"""
import asyncio

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import federation
from app import app
from core import pack_fetch
from database import Base, get_db
from models import ArtworkModel, PlaylistModel, SettingsModel, SubscriptionModel, playlist_artwork
from routers import packs as packs_router

REG_URL = "https://packs.test/packs.json"


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


def test_list_packs_demo_mode_masks_raw_error(client, monkeypatch):
    """A reviewer flagged the raw exception text as too much detail for an anonymous demo caller."""
    import config
    monkeypatch.setattr(config, "DEMO_MODE", True)
    packs_router._demo_cache["body"] = None  # a prior test's cached body must not short-circuit this one
    packs_router._demo_cache["expires"] = 0.0

    async def boom(_c, _u):
        raise RuntimeError("some internal detail")
    monkeypatch.setattr(pack_fetch, "fetch_registry", boom)
    d = client.get("/api/packs").json()
    assert d["error"] == "registry unavailable"
    assert "some internal detail" not in d["error"]


def test_list_packs_demo_mode_caches_across_calls(client, monkeypatch):
    """GET /api/packs is on the demo allowlist and hit by every visitor's browse-card load; demo mode
    must serve a 10-minute TTL cache rather than fetching the registry on every request."""
    import config
    monkeypatch.setattr(config, "DEMO_MODE", True)
    packs_router._demo_cache["body"] = None
    packs_router._demo_cache["expires"] = 0.0

    calls = []

    async def fake_fetch(_c, _u):
        calls.append(1)
        return _fake_registry()
    monkeypatch.setattr(pack_fetch, "fetch_registry", fake_fetch)

    first = client.get("/api/packs").json()
    second = client.get("/api/packs").json()
    assert len(calls) == 1  # second call served from cache, no re-fetch
    assert first == second

    packs_router._demo_cache["body"] = None
    packs_router._demo_cache["expires"] = 0.0


def test_install_endpoint_starts_and_dedups(client, db, monkeypatch):
    async def fake_fetch(_c, _u):
        return _fake_registry()
    monkeypatch.setattr(pack_fetch, "fetch_registry", fake_fetch)
    async def fake_install(_db, _client, _url, _cid):
        return {"ok": True, "trust": "verified", "installed": True}
    monkeypatch.setattr(pack_fetch, "install_collection_from_registry", fake_install)
    monkeypatch.setattr(packs_router, "SessionLocal", lambda: db)

    assert client.post("/api/packs/cartography/install").json()["state"] == "started"
    # a second request while one is in flight is a no-op
    packs_router._JOBS["cosmos"] = {"state": "in_progress"}
    assert client.post("/api/packs/cosmos/install").json()["state"] == "in_progress"
    assert client.get("/api/packs/status").json()["cosmos"]["state"] == "in_progress"


@respx.mock
def test_install_unknown_id_is_404_and_starts_no_task(client, db, monkeypatch):
    monkeypatch.setattr(federation, "_assert_public_url", lambda url: None)  # test host isn't public
    respx.get(REG_URL).respond(200, json=_fake_registry())
    started = []
    monkeypatch.setattr(packs_router, "_install_job", lambda *a: started.append(a))
    db.add(SettingsModel(setting_key="pack_registry_url", setting_value=REG_URL))
    db.commit()

    r = client.post("/api/packs/no-such-pack/install")
    assert r.status_code == 404 and "no-such-pack" in r.json()["detail"]
    assert started == [] and "no-such-pack" not in packs_router._JOBS


@respx.mock
def test_install_known_id_starts(client, db, monkeypatch):
    monkeypatch.setattr(federation, "_assert_public_url", lambda url: None)  # test host isn't public
    respx.get(REG_URL).respond(200, json=_fake_registry())
    async def fake_install(_db, _client, _url, _cid):
        return {"ok": True, "trust": "verified", "installed": True}
    monkeypatch.setattr(pack_fetch, "install_collection_from_registry", fake_install)
    monkeypatch.setattr(packs_router, "SessionLocal", lambda: db)
    db.add(SettingsModel(setting_key="pack_registry_url", setting_value=REG_URL))
    db.commit()

    r = client.post("/api/packs/cosmos/install")
    assert r.status_code == 200 and r.json() == {"state": "started"}


@respx.mock
def test_install_registry_unreachable_is_502_not_started(client, db, monkeypatch):
    monkeypatch.setattr(federation, "_assert_public_url", lambda url: None)  # test host isn't public
    respx.get(REG_URL).respond(500)
    started = []
    monkeypatch.setattr(packs_router, "_install_job", lambda *a: started.append(a))
    db.add(SettingsModel(setting_key="pack_registry_url", setting_value=REG_URL))
    db.commit()

    r = client.post("/api/packs/cosmos/install")
    assert r.status_code == 502 and "registry" in r.json()["detail"]
    assert started == []


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


@respx.mock
def test_install_double_call_starts_one_job(client, db, monkeypatch):
    """The slot is claimed before the registry await, so a second request is in_progress, not a 2nd install."""
    monkeypatch.setattr(federation, "_assert_public_url", lambda url: None)
    respx.get(REG_URL).respond(200, json=_fake_registry())
    started = []

    async def fake_job(*a):
        started.append(a)
    monkeypatch.setattr(packs_router, "_install_job", fake_job)
    db.add(SettingsModel(setting_key="pack_registry_url", setting_value=REG_URL))
    db.commit()

    assert client.post("/api/packs/cosmos/install").json()["state"] == "started"
    assert packs_router._JOBS["cosmos"]["state"] == "in_progress"  # claimed by the endpoint itself
    assert client.post("/api/packs/cosmos/install").json()["state"] == "in_progress"
    assert len(started) == 1


@respx.mock
def test_install_rejection_clears_the_claimed_slot(client, db, monkeypatch):
    monkeypatch.setattr(federation, "_assert_public_url", lambda url: None)
    respx.get(REG_URL).respond(200, json=_fake_registry())
    db.add(SettingsModel(setting_key="pack_registry_url", setting_value=REG_URL))
    db.commit()
    assert client.post("/api/packs/nope/install").status_code == 404
    assert "nope" not in packs_router._JOBS


@respx.mock
@pytest.mark.parametrize("body", [{"collections": ["cosmos"]}, ["cosmos"], {"collections": None}])
def test_install_malformed_registry_is_502_and_releases_slot(client, db, monkeypatch, body):
    monkeypatch.setattr(federation, "_assert_public_url", lambda url: None)
    respx.get(REG_URL).respond(200, json=body)
    db.add(SettingsModel(setting_key="pack_registry_url", setting_value=REG_URL))
    db.commit()
    assert client.post("/api/packs/cosmos/install").status_code == 502
    assert "cosmos" not in packs_router._JOBS


@pytest.mark.asyncio
async def test_install_cancellation_releases_slot(db, monkeypatch):
    async def hang(_c, _u):
        raise asyncio.CancelledError
    monkeypatch.setattr(pack_fetch, "fetch_registry", hang)
    with pytest.raises(asyncio.CancelledError):
        await packs_router.install_pack("cosmos", db)
    assert "cosmos" not in packs_router._JOBS
