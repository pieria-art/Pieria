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
    monkeypatch.setattr(L, "SessionLocal", lambda: _db())

    seen = {}

    async def _ok(url, cid):
        seen["url"], seen["cid"] = url, cid
        return True
    monkeypatch.setattr(L, "_seed_default_collection", _ok)

    ok = await L.seed_from_registry(db)
    await asyncio.sleep(0)  # let the scheduled task run
    assert ok is True
    assert seen["cid"] == "masterpieces"


@pytest.mark.asyncio
async def test_resolve_default_returns_none_when_registry_unreachable(monkeypatch):
    async def boom(_c, _u):
        raise RuntimeError("dns fail")
    monkeypatch.setattr(pack_fetch, "fetch_registry", boom)
    assert await L._resolve_default_collection("https://packs.test/packs.json") is None


@pytest.mark.asyncio
async def test_seed_starts_the_retry_loop_even_when_registry_is_down(monkeypatch):
    """ADR-061: an unreachable registry on first boot must NOT end the out-of-box seed — the box used
    to sit there with nothing until someone power-cycled it."""
    db = _db()

    async def boom(_c, _u):
        raise RuntimeError("dns fail")
    monkeypatch.setattr(pack_fetch, "fetch_registry", boom)
    monkeypatch.setattr(L, "SessionLocal", lambda: _db())

    started = asyncio.Event()

    async def _loop(url):
        started.set()
    monkeypatch.setattr(L, "_oob_seed_loop", _loop)

    assert await L.seed_from_registry(db) is True  # seeding is UNDERWAY, not abandoned
    await asyncio.wait_for(started.wait(), timeout=1)


@pytest.mark.asyncio
async def test_retry_loop_keeps_going_until_the_pull_lands(monkeypatch):
    """The network comes up mid-retry: the first two attempts fail, the third succeeds, loop exits."""
    db = _db()
    monkeypatch.setattr(L, "SessionLocal", lambda: db)
    monkeypatch.setattr(L, "_SEED_RETRY_BACKOFF", (0,))  # no real sleeping in tests

    async def fake_reg(_c, _u):
        return {"default": "masterpieces", "collections": [{"id": "masterpieces"}]}
    monkeypatch.setattr(pack_fetch, "fetch_registry", fake_reg)

    attempts = []

    async def flaky(_url, cid):
        attempts.append(cid)
        return len(attempts) >= 3
    monkeypatch.setattr(L, "_seed_default_collection", flaky)

    await asyncio.wait_for(L._oob_seed_loop("https://packs.test/packs.json"), timeout=5)
    assert attempts == ["masterpieces"] * 3


@pytest.mark.asyncio
async def test_retry_loop_survives_a_dead_registry_then_recovers(monkeypatch):
    """A registry that is unreachable (not merely a failed download) must also be retried, not fatal."""
    db = _db()
    monkeypatch.setattr(L, "SessionLocal", lambda: db)
    monkeypatch.setattr(L, "_SEED_RETRY_BACKOFF", (0,))

    calls = []

    async def flaky_reg(_c, _u):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("dns fail")
        return {"default": "masterpieces", "collections": [{"id": "masterpieces"}]}
    monkeypatch.setattr(pack_fetch, "fetch_registry", flaky_reg)

    async def _ok(_url, _cid):
        return True
    monkeypatch.setattr(L, "_seed_default_collection", _ok)

    await asyncio.wait_for(L._oob_seed_loop("https://packs.test/packs.json"), timeout=5)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_retry_loop_stands_down_when_already_seeded(monkeypatch):
    db = _db()
    db.add(SettingsModel(setting_key="pack_seeded", setting_value="v2:registry"))
    db.commit()
    monkeypatch.setattr(L, "SessionLocal", lambda: db)

    async def nope(_c, _u):
        raise AssertionError("must not touch the registry once seeded")
    monkeypatch.setattr(pack_fetch, "fetch_registry", nope)

    await asyncio.wait_for(L._oob_seed_loop("https://packs.test/packs.json"), timeout=5)


@pytest.mark.asyncio
async def test_retry_loop_stands_down_when_the_user_installs_a_pack(monkeypatch):
    """The owner got impatient and downloaded a collection from the Art Packs card mid-retry. The loop
    must notice, stop, and leave complete state behind (seeded marker + a default playlist)."""
    db = _db()
    monkeypatch.setattr(L, "SessionLocal", lambda: db)
    monkeypatch.setattr(L, "_SEED_RETRY_BACKOFF", (0,))

    async def fake_reg(_c, _u):
        return {"default": "masterpieces", "collections": [{"id": "masterpieces"}]}
    monkeypatch.setattr(pack_fetch, "fetch_registry", fake_reg)

    async def user_installs_meanwhile(_url, _cid):
        db.add(SubscriptionModel(url="pack:ukiyo-e", title="Ukiyo-e", trust="verified"))
        db.commit()
        return False  # our own attempt still failed
    monkeypatch.setattr(L, "_seed_default_collection", user_installs_meanwhile)

    await asyncio.wait_for(L._oob_seed_loop("https://packs.test/packs.json"), timeout=5)

    seeded = db.query(SettingsModel).filter(SettingsModel.setting_key == "pack_seeded").first()
    default = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    assert seeded.setting_value == "v2:user"
    assert default.setting_value == "Ukiyo-e"


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
