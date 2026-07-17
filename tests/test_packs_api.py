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
from models import SubscriptionModel
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
        {"id": "masterpieces", "title": "Masterpieces", "category": "featured", "item_count": 40, "bytes": 3, "core": True},
        {"id": "cartography", "title": "Cartography", "category": "map", "item_count": 20, "bytes": 2, "core": False},
        {"id": "cosmos", "title": "Cosmos", "category": "photo", "item_count": 15, "bytes": 1, "core": False},
    ]}


def test_list_packs_annotates_installed(client, db, monkeypatch):
    async def fake_fetch(_c, _u):
        return _fake_registry()
    monkeypatch.setattr(pack_fetch, "fetch_registry", fake_fetch)
    db.add(SubscriptionModel(url="pack:cartography", title="Cartography"))
    db.commit()

    d = client.get("/api/packs").json()
    assert d["core"] == ["masterpieces"]
    by = {c["id"]: c for c in d["collections"]}
    assert by["cartography"]["installed"] is True
    assert by["cosmos"]["installed"] is False
    assert by["masterpieces"]["core"] is True and by["cartography"]["category"] == "map"


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
