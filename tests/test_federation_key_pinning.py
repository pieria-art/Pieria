"""L5 / ADR-148: community publisher keys are pinned across federation syncs (SSH-style TOFU)."""
import base64
import json

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
import federation
from app import app
from database import Base, get_db
from models import SubscriptionModel

URL = "https://jane.test/m.json"


def _signed(key, title="Jane", pub_id="jane"):
    m = {"manifest_version": 2, "id": "jane-c", "title": title,
         "publisher": {"id": pub_id, "name": "Jane", "url": "https://jane.test"},
         "items": [{"id": "a1", "title": title, "artist": "Monet",
                    "image": {"full_url": "https://cdn.jane.test/a.jpg", "license": "CC0-1.0"}}]}
    if key is not None:
        m["publisher"]["public_key"] = base64.b64encode(bytes(key.verify_key)).decode()
        m["signature"] = base64.b64encode(key.sign(federation.canonical_bytes(m)).signature).decode()
    return m


def _b64(key):
    return base64.b64encode(bytes(key.verify_key)).decode()


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    state = {"m": None}

    async def _fetch(url):
        return state["m"]
    monkeypatch.setattr(app_module.federation, "fetch_manifest", _fetch)
    with TestClient(app) as c:
        yield c, db, state
    app.dependency_overrides.clear()
    db.close()


def _subscribe(c, state, manifest):
    state["m"] = manifest
    r = c.post("/api/subscriptions", json={"url": URL})
    assert r.status_code == 200, r.text
    return r.json()


def _sync(c, sid):
    return c.post(f"/api/subscriptions/{sid}/sync").json()


def _cached_title(db, sid):
    db.expire_all()
    return json.loads(db.get(SubscriptionModel, sid).cached_manifest)["title"]


def test_pin_at_subscribe(env):
    c, db, st = env
    k = SigningKey.generate()
    sub = _subscribe(c, st, _signed(k))
    assert db.get(SubscriptionModel, sub["id"]).pinned_public_key == _b64(k)
    assert sub["key_status"] == "ok" and sub["pinned_fingerprint"]


def test_unsigned_subscribe_pins_nothing(env):
    c, db, st = env
    sub = _subscribe(c, st, _signed(None))
    assert db.get(SubscriptionModel, sub["id"]).pinned_public_key is None


def test_unchanged_key_syncs(env):
    c, db, st = env
    k = SigningKey.generate()
    sub = _subscribe(c, st, _signed(k, title="One"))
    st["m"] = _signed(k, title="Two")
    assert _sync(c, sub["id"])["last_status"] == "ok"
    assert _cached_title(db, sub["id"]) == "Two"


def test_changed_key_blocks_and_preserves_last_good(env):
    c, db, st = env
    k1, k2 = SigningKey.generate(), SigningKey.generate()
    sub = _subscribe(c, st, _signed(k1, title="Good"))
    st["m"] = _signed(k2, title="Evil")
    out = _sync(c, sub["id"])
    assert out["key_status"] == "changed" and out["last_status"].startswith("blocked")
    assert out["pending_fingerprint"] != out["pinned_fingerprint"]
    assert _cached_title(db, sub["id"]) == "Good"
    assert db.get(SubscriptionModel, sub["id"]).pending_public_key == _b64(k2)
    assert c.get(f"/api/catalog/{sub['collection_id']}").json()["items"][0]["title"] == "Good"


def test_signature_stripped_blocks(env):
    c, db, st = env
    k = SigningKey.generate()
    sub = _subscribe(c, st, _signed(k, title="Good"))
    st["m"] = _signed(None, title="Stripped")
    out = _sync(c, sub["id"])
    assert out["key_status"] == "changed"
    assert _cached_title(db, sub["id"]) == "Good"


def test_unsigned_then_signed_pins(env):
    c, db, st = env
    sub = _subscribe(c, st, _signed(None))
    k = SigningKey.generate()
    st["m"] = _signed(k, title="Now signed")
    assert _sync(c, sub["id"])["key_status"] == "ok"
    assert db.get(SubscriptionModel, sub["id"]).pinned_public_key == _b64(k)
    assert _cached_title(db, sub["id"]) == "Now signed"


def test_retrust_promotes_and_syncs(env):
    c, db, st = env
    k1, k2 = SigningKey.generate(), SigningKey.generate()
    sub = _subscribe(c, st, _signed(k1, title="Good"))
    st["m"] = _signed(k2, title="New owner")
    _sync(c, sub["id"])
    r = c.post(f"/api/subscriptions/{sub['id']}/retrust")
    assert r.status_code == 200 and r.json()["key_status"] == "ok"
    db.expire_all()
    row = db.get(SubscriptionModel, sub["id"])
    assert row.pinned_public_key == _b64(k2) and row.pending_public_key is None
    assert _cached_title(db, sub["id"]) == "New owner"


def test_retrust_refuses_if_feed_changed_again(env):
    c, db, st = env
    k1, k2, k3 = (SigningKey.generate() for _ in range(3))
    sub = _subscribe(c, st, _signed(k1))
    st["m"] = _signed(k2)
    _sync(c, sub["id"])
    st["m"] = _signed(k3)        # a third key appears before the user clicks Trust
    out = c.post(f"/api/subscriptions/{sub['id']}/retrust").json()
    assert out["key_status"] == "changed"
    db.expire_all()
    assert db.get(SubscriptionModel, sub["id"]).pinned_public_key == _b64(k1)


def test_retrust_with_nothing_flagged_is_400(env):
    c, db, st = env
    sub = _subscribe(c, st, _signed(SigningKey.generate()))
    assert c.post(f"/api/subscriptions/{sub['id']}/retrust").status_code == 400


def test_registry_rotation_follows_registry_and_flags(env, monkeypatch):
    c, db, st = env
    k1, k2 = SigningKey.generate(), SigningKey.generate()
    monkeypatch.setattr(federation, "TRUSTED_KEYS", {"jane": _b64(k1)})
    sub = _subscribe(c, st, _signed(k1, title="Old"))
    assert sub["trust"] == "verified"
    monkeypatch.setattr(federation, "TRUSTED_KEYS", {"jane": _b64(k2)})   # registry record rotated
    st["m"] = _signed(k2, title="Rotated")
    out = _sync(c, sub["id"])
    assert out["key_status"] == "rotated" and out["trust"] == "verified"
    assert _cached_title(db, sub["id"]) == "Rotated"
    row = db.get(SubscriptionModel, sub["id"])
    assert row.pinned_public_key == _b64(k2) and row.pending_public_key == _b64(k1)
    assert c.post(f"/api/subscriptions/{sub['id']}/retrust").json()["key_status"] == "ok"
