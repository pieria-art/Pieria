"""ADR-040 #4 OOB "art in 5 minutes": seed_from_registry pulls the registry's default collection from R2
on a fresh boot (no baked pack), installs it, and sets it as the default playlist. Falls back to the live
factory seed only when the registry is unreachable.
"""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core import lifespan as L
from core import pack_fetch
from database import Base
from models import PlaylistModel, SettingsModel, SubscriptionModel


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.mark.asyncio
async def test_seed_schedules_default_when_registry_ok(monkeypatch):
    db = _db()

    async def fake_reg(_c, _u):
        return {"default": "masterpieces", "core": ["masterpieces"], "collections": [{"id": "masterpieces"}]}
    monkeypatch.setattr(pack_fetch, "fetch_registry", fake_reg)

    seen = {}

    async def _noop(url, cid):
        seen["url"], seen["cid"] = url, cid
    monkeypatch.setattr(L, "_seed_default_collection", _noop)

    ok = await L.seed_from_registry(db)
    await asyncio.sleep(0)  # let the scheduled task run
    assert ok is True
    assert seen["cid"] == "masterpieces"


@pytest.mark.asyncio
async def test_seed_falls_back_when_registry_unreachable(monkeypatch):
    db = _db()

    async def boom(_c, _u):
        raise RuntimeError("dns fail")
    monkeypatch.setattr(pack_fetch, "fetch_registry", boom)
    assert await L.seed_from_registry(db) is False  # caller then runs the factory seed


@pytest.mark.asyncio
async def test_seed_idempotent_when_already_seeded(monkeypatch):
    db = _db()
    db.add(SettingsModel(setting_key="pack_seeded", setting_value="v2:registry"))
    db.commit()

    async def nope(_c, _u):
        raise AssertionError("must not fetch the registry when already seeded")
    monkeypatch.setattr(pack_fetch, "fetch_registry", nope)
    assert await L.seed_from_registry(db) is True


@pytest.mark.asyncio
async def test_seed_default_collection_installs_and_sets_default_playlist(monkeypatch):
    db = _db()

    async def fake_install(_db, _client, _url, cid):
        _db.add(SubscriptionModel(url=f"pack:{cid}", title="Masterpieces", trust="verified"))
        _db.add(PlaylistModel(name="Masterpieces", is_personal=False))
        _db.commit()
        return {"ok": True, "trust": "verified", "installed": True}
    monkeypatch.setattr(pack_fetch, "install_collection_from_registry", fake_install)
    monkeypatch.setattr(L, "SessionLocal", lambda: db)

    await L._seed_default_collection("https://packs.test/packs.json", "masterpieces")

    default = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    seeded = db.query(SettingsModel).filter(SettingsModel.setting_key == "pack_seeded").first()
    assert default.setting_value == "Masterpieces"
    assert seeded.setting_value == "v2:registry"
