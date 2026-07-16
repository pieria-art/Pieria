"""Publisher Studio API — identity (key handling), collection CRUD, SSRF guard, validate, export."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
import federation
from app import app
from database import Base, get_db


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    # Test image URLs use the reserved .test TLD (won't resolve) — neutralize the live-DNS SSRF guard
    # by default; the dedicated SSRF test re-patches it to block.
    monkeypatch.setattr(app_module.federation, "_assert_public_url", lambda url: None)
    with TestClient(app) as c:
        yield c, db
    app.dependency_overrides.clear()
    db.close()


def _identity(c):
    return c.post("/api/publisher/identity", json={"id": "jane", "name": "Jane Doe", "url": "https://jane.test"})


def _item(**over):
    base = {"title": "Sunrise", "full_url": "https://cdn.jane.test/a.jpg", "license": "CC0-1.0"}
    base.update(over)
    return base


# --- identity ---------------------------------------------------------------

def test_identity_creates_key_and_never_leaks_private(client):
    c, _ = client
    r = _identity(c)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["id"] == "jane" and d["has_private_key"] is True and d["public_key"]
    assert "private" not in json.dumps(d).lower() or "has_private_key" in d  # no raw private key field
    assert "publisher_private_key" not in d
    # GET likewise never returns the private key
    g = c.get("/api/publisher/identity").json()
    assert g["has_private_key"] is True and "publisher_private_key" not in g


def test_identity_keeps_key_without_regenerate_but_rotates_with_it(client):
    c, _ = client
    k1 = _identity(c).json()["public_key"]
    k2 = _identity(c).json()["public_key"]            # second save, no regenerate
    assert k1 == k2
    r = c.post("/api/publisher/identity",
               json={"id": "jane", "name": "Jane Doe", "regenerate": True})
    body = r.json()
    assert body["public_key"] != k1 and "warning" in body


# --- collection CRUD + slug -------------------------------------------------

def test_collection_crud_and_slug_dedupe(client):
    c, _ = client
    a = c.post("/api/publisher/collections", json={"title": "My Art", "items": []}).json()
    b = c.post("/api/publisher/collections", json={"title": "My Art", "items": []}).json()
    assert a["slug"] == "my-art" and b["slug"] == "my-art-2"   # deduped

    lst = c.get("/api/publisher/collections").json()
    assert len(lst) == 2

    upd = c.put(f"/api/publisher/collections/{a['id']}",
                json={"title": "My Art", "default_license": "CC0-1.0", "items": [_item()]}).json()
    assert upd["default_license"] == "CC0-1.0" and len(upd["items"]) == 1
    assert upd["items"][0]["image"]["full_url"] == "https://cdn.jane.test/a.jpg"

    assert c.delete(f"/api/publisher/collections/{b['id']}").status_code == 200
    assert len(c.get("/api/publisher/collections").json()) == 1


def test_cover_image_persists_and_exports(client):
    c, _ = client
    _identity(c)
    cover = "https://cdn.jane.test/cover.jpg"
    col = c.post("/api/publisher/collections",
                 json={"title": "X", "default_license": "CC0-1.0", "cover_image": cover,
                       "items": [_item()]}).json()
    assert col["cover_image"] == cover
    manifest = json.loads(c.post(f"/api/publisher/collections/{col['id']}/export").content)
    assert manifest["cover_image"] == cover
    from manifest_validator import validate_manifest
    assert validate_manifest(manifest) == []


def test_ssrf_guard_rejects_nonpublic_image_url(client, monkeypatch):
    c, _ = client
    def _block(url):
        raise federation.FederationError("non-public")
    monkeypatch.setattr(app_module.federation, "_assert_public_url", _block)
    r = c.post("/api/publisher/collections",
               json={"title": "X", "items": [_item(full_url="http://169.254.169.254/meta.jpg")]})
    assert r.status_code == 400 and "rejected" in r.json()["detail"].lower()


# --- validate + export ------------------------------------------------------

def test_validate_reports_errors(client):
    c, _ = client
    col = c.post("/api/publisher/collections",
                 json={"title": "X", "items": [_item(license="CC-BY-4.0")]}).json()  # missing attribution
    d = c.post(f"/api/publisher/collections/{col['id']}/validate").json()
    assert d["valid"] is False and any("attribution" in e for e in d["errors"])


def test_export_requires_identity(client):
    c, _ = client
    col = c.post("/api/publisher/collections", json={"title": "X", "items": [_item()]}).json()
    assert c.post(f"/api/publisher/collections/{col['id']}/export").status_code == 400


def test_export_returns_signed_verifiable_manifest(client):
    c, _ = client
    _identity(c)
    col = c.post("/api/publisher/collections",
                 json={"title": "Janes Art", "default_license": "CC0-1.0", "items": [_item()]}).json()
    r = c.post(f"/api/publisher/collections/{col['id']}/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].endswith(f'"{col["slug"]}.json"')
    manifest = json.loads(r.content)
    from manifest_validator import validate_manifest
    assert validate_manifest(manifest) == []
    assert federation.verify_signature(manifest) is True
    assert manifest["publisher"]["id"] == "jane"


def test_export_invalid_manifest_returns_422(client):
    c, _ = client
    _identity(c)
    col = c.post("/api/publisher/collections",
                 json={"title": "X", "items": [_item(license="CC-BY-4.0")]}).json()  # no attribution
    r = c.post(f"/api/publisher/collections/{col['id']}/export")
    assert r.status_code == 422


# --- dogfood: a Studio-exported manifest survives the subscriber's verify path ---

def test_dogfood_exported_manifest_is_subscribable(client):
    c, _ = client
    _identity(c)
    col = c.post("/api/publisher/collections",
                 json={"title": "Janes Art", "default_license": "CC0-1.0", "items": [_item()]}).json()
    manifest = json.loads(c.post(f"/api/publisher/collections/{col['id']}/export").content)

    # community tier with no registry entry; verified once the key is trusted
    assert federation.assess_trust(manifest, trusted_keys={}) == "community"
    trusted = {manifest["publisher"]["id"]: manifest["publisher"]["public_key"]}
    assert federation.assess_trust(manifest, trusted_keys=trusted) == "verified"
