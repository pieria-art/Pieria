"""Federation — SSRF guard, safe fetch/validate, and the subscribe-by-URL endpoints + catalog merge."""

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
from federation import FederationError, _assert_public_url, fetch_manifest, manifest_item_to_catalog


def _valid_manifest():
    return {
        "manifest_version": 2,
        "id": "janes-impressionists",
        "title": "Jane's Impressionists",
        "publisher": {"id": "jane", "name": "Jane's Gallery", "url": "https://jane.test"},
        "items": [{
            "id": "a1", "title": "Sunrise", "artist": "Monet",
            "image": {"full_url": "https://cdn.jane.test/a.jpg", "thumbnail_url": "https://cdn.jane.test/a-t.jpg",
                      "license": "CC0-1.0"},
        }],
    }


def _gai(ip):
    # getaddrinfo returns (family, type, proto, canonname, sockaddr=(ip, port))
    return lambda *a, **k: [(2, 1, 6, "", (ip, 443))]


# --- SSRF guard -------------------------------------------------------------

@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1"])
def test_ssrf_guard_blocks_non_public(monkeypatch, ip):
    monkeypatch.setattr(federation.socket, "getaddrinfo", _gai(ip))
    with pytest.raises(FederationError):
        _assert_public_url("https://evil.test/m.json")


def test_ssrf_guard_allows_public(monkeypatch):
    monkeypatch.setattr(federation.socket, "getaddrinfo", _gai("93.184.216.34"))
    _assert_public_url("https://example.test/m.json")  # no raise


def test_non_http_scheme_rejected():
    with pytest.raises(FederationError):
        _assert_public_url("ftp://example.test/m.json")
    with pytest.raises(FederationError):
        _assert_public_url("file:///etc/passwd")


# --- fetch_manifest (mock the network, keep validation/safety real) ---------

class _FakeStream:
    def __init__(self, status=200, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {"content-type": "application/json"}
        self._body = body

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def aiter_bytes(self):
        yield self._body


def _patch_fetch(monkeypatch, stream):
    monkeypatch.setattr(federation, "_assert_public_url", lambda url: None)  # bypass DNS in these tests

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, method, url, **k): return stream
    monkeypatch.setattr(federation.httpx, "AsyncClient", _Client)


@pytest.mark.asyncio
async def test_fetch_manifest_valid(monkeypatch):
    _patch_fetch(monkeypatch, _FakeStream(body=json.dumps(_valid_manifest()).encode()))
    m = await fetch_manifest("https://jane.test/m.json")
    assert m["id"] == "janes-impressionists"


@pytest.mark.asyncio
async def test_fetch_manifest_rejects_html(monkeypatch):
    _patch_fetch(monkeypatch, _FakeStream(headers={"content-type": "text/html"}, body=b"<html>"))
    with pytest.raises(FederationError):
        await fetch_manifest("https://jane.test/m.json")


@pytest.mark.asyncio
async def test_fetch_manifest_rejects_redirect(monkeypatch):
    _patch_fetch(monkeypatch, _FakeStream(status=302))
    with pytest.raises(FederationError):
        await fetch_manifest("https://jane.test/m.json")


@pytest.mark.asyncio
async def test_fetch_manifest_rejects_oversize(monkeypatch):
    monkeypatch.setattr(federation, "MAX_MANIFEST_BYTES", 10)
    _patch_fetch(monkeypatch, _FakeStream(body=b"x" * 50))
    with pytest.raises(FederationError):
        await fetch_manifest("https://jane.test/m.json")


@pytest.mark.asyncio
async def test_fetch_manifest_rejects_invalid_schema(monkeypatch):
    bad = {"manifest_version": 2, "id": "x"}  # missing title + items
    _patch_fetch(monkeypatch, _FakeStream(body=json.dumps(bad).encode()))
    with pytest.raises(FederationError):
        await fetch_manifest("https://jane.test/m.json")


# --- mapping ----------------------------------------------------------------

def test_manifest_item_to_catalog_maps_fields():
    item = _valid_manifest()["items"][0]
    c = manifest_item_to_catalog(item)
    assert c["title"] == "Sunrise" and c["agent_name"] == "Monet"
    assert c["source_url"] == "https://cdn.jane.test/a.jpg"
    assert c["thumbnail_url"] == "https://cdn.jane.test/a-t.jpg"
    assert c["license"] == "CC0-1.0"


def test_manifest_item_to_catalog_maps_series_and_resolution_tier():
    item = {**_valid_manifest()["items"][0], "series": "Eastern Capital", "resolution_tier": "8K"}
    c = manifest_item_to_catalog(item)
    assert c["series"] == "Eastern Capital" and c["resolution_tier"] == "8K"
    # absent → None (install stores NULL, not "")
    bare = manifest_item_to_catalog(_valid_manifest()["items"][0])
    assert bare["series"] is None and bare["resolution_tier"] is None


# --- endpoints + catalog merge ----------------------------------------------

@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db

    async def _fake_fetch(url):
        if "bad" in url:
            raise FederationError("nope")
        return _valid_manifest()
    monkeypatch.setattr(app_module.federation, "fetch_manifest", _fake_fetch)

    with TestClient(app) as c:
        yield c, db
    app.dependency_overrides.clear()
    db.close()


def test_subscribe_flow_and_merge(client):
    c, db = client
    # subscribe
    r = c.post("/api/subscriptions", json={"url": "https://jane.test/m.json"})
    assert r.status_code == 200, r.text
    sub = r.json()
    assert sub["trust"] == "community" and sub["item_count"] == 1
    cid = sub["collection_id"]  # 'sub_1'

    # appears in the catalog index, tagged as a subscription (not bundled)
    idx = c.get("/api/catalog").json()
    mine = [col for col in idx["collections"] if col["id"] == cid]
    assert mine and mine[0]["origin"] == "subscription" and mine[0]["trust"] == "community"

    # its items resolve, mapped to the catalog shape
    col = c.get(f"/api/catalog/{cid}").json()
    assert col["origin"] == "subscription"
    assert col["items"][0]["title"] == "Sunrise"

    # duplicate URL rejected
    assert c.post("/api/subscriptions", json={"url": "https://jane.test/m.json"}).status_code == 409

    # delete
    assert c.delete(f"/api/subscriptions/{sub['id']}").status_code == 200
    assert c.get("/api/subscriptions").json() == []


def test_subscribe_rejects_bad_manifest(client):
    c, _ = client
    r = c.post("/api/subscriptions", json={"url": "https://bad.test/m.json"})
    assert r.status_code == 400


def test_add_from_subscription_ssrf_guarded(client, monkeypatch):
    c, db = client
    c.post("/api/subscriptions", json={"url": "https://jane.test/m.json"})
    # Force the image URL to look non-public when the add flow guards it.
    def _block(url):
        raise FederationError("non-public")
    monkeypatch.setattr(app_module.federation, "_assert_public_url", _block)
    r = c.post("/api/catalog/add", json={"collection_id": "sub_1", "item_index": 0})
    assert r.status_code == 400 and "Refused" in r.json()["detail"]
