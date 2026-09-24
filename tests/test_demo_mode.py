"""Public demo mode (SD_DEMO_MODE=1, core/demo.py) — the default-DENY gate, display-id
normalization, WS cap, /api/demo, and SD_DEMO_PACKS bootstrap."""

import fastapi.routing as fastapi_routing
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.routing import Mount, Route, WebSocketRoute

import config
import core.demo as core_demo
from app import app
from database import Base, get_db
from models import PlaylistModel, SubscriptionModel


@pytest.fixture
def demo_client(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_client(monkeypatch):
    """A demo-mode client with an isolated in-memory DB, for routes that need one."""
    monkeypatch.setattr(config, "DEMO_MODE", True)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c, db
    app.dependency_overrides.clear()
    db.close()


# --- Route enumeration (mirrors the introspection FastAPI itself does at request time) ------------
# FastAPI's `app.include_router` wraps each router as a lazily-resolved `_IncludedRouter`; walk its
# effective candidates to recover the concrete (methods, path) pairs. This is what makes the sweep
# below a REAL guard against a future route landing with no allowlist decision.
def _enumerate_routes():
    out = []

    def walk(routes):
        for r in routes:
            if isinstance(r, fastapi_routing._IncludedRouter):
                walk(r.effective_candidates() + r.effective_low_priority_routes())
            elif hasattr(r, "starlette_route"):  # an _EffectiveRouteContext
                sr = r.starlette_route
                if isinstance(sr, WebSocketRoute):
                    out.append((("WS",), sr.path))
                else:
                    out.append((tuple(sorted((r.methods or set()) - {"HEAD", "OPTIONS"})), r.path))
            elif isinstance(r, Route):
                out.append((tuple(sorted(r.methods - {"HEAD", "OPTIONS"})), r.path))
            elif isinstance(r, WebSocketRoute):
                out.append((("WS",), r.path))
            elif isinstance(r, Mount):
                pass  # static mounts — not FastAPI routes, handled by core.demo._is_static_asset

    walk(app.routes)
    return sorted(set(out))


def _concretize(path: str) -> str:
    """Fill every {param} with a harmless dummy value so the path can actually be requested."""
    import re
    return re.sub(r"\{[^}]+\}", "1", path)


def test_default_deny_sweep(demo_client):
    """Every route NOT on core.demo.ALLOWED_ROUTES must 403 in demo mode — the guard against a new
    route landing without an allowlist decision. WS routes are checked separately."""
    for methods, path in _enumerate_routes():
        if methods == ("WS",):
            continue
        concrete = _concretize(path)
        for method in methods:
            resp = demo_client.request(method, concrete)
            allowed = core_demo._is_allowed_http(method, path) or (
                method in ("GET", "HEAD") and core_demo._is_static_asset(path))
            if allowed:
                assert resp.status_code != 403, f"{method} {path} unexpectedly gated"
            else:
                assert resp.status_code == 403, f"{method} {path} should be demo-denied, got {resp.status_code}"
                assert "install your own" in resp.json()["detail"]


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin"),
    ("GET", "/help"),
    ("GET", "/playlists"),
    ("GET", "/artworks"),
    ("GET", "/api/catalog"),
    ("GET", "/api/packs"),
    ("GET", "/api/packs/status"),
    ("GET", "/api/remote/displays"),
    ("GET", "/api/demo"),
    ("GET", "/next-image"),  # missing playlist_name -> 422, not 403 — proves the gate let it through
])
def test_allowed_routes_pass_the_gate(demo_client, method, path):
    resp = demo_client.request(method, path)
    assert resp.status_code != 403


@pytest.mark.parametrize("path", [
    "/api/catalog/search",
    "/api/catalog/suggest",
    "/studio",
    "/remote",
    "/publisher",
    "/artworks/pending",
    "/display/default/current.png",
    "/api/health/host",
    "/docs",
    "/redoc",
    "/openapi.json",
])
def test_explicitly_excluded_routes_are_denied(demo_client, path):
    assert demo_client.get(path).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("POST", "/upload"),
    ("POST", "/playlists"),
    ("DELETE", "/artworks/1"),
    ("PATCH", "/artworks/1/approve"),
    ("POST", "/api/catalog/add"),
    ("POST", "/api/admin/factory-reset"),
    ("POST", "/api/appliance/update"),
])
def test_mutations_denied_except_heartbeat(demo_client, method, path):
    assert demo_client.request(method, path).status_code == 403


def test_heartbeat_is_a_noop_204_in_demo(db_client):
    demo, db = db_client
    from models import ArtworkModel
    art = ArtworkModel(filename="x.jpg", status="approved", total_display_time=0)
    db.add(art); db.commit(); db.refresh(art)
    resp = demo.post("/api/telemetry/heartbeat",
                      json={"artwork_id": art.id, "display_time_sec": 30, "skipped": False})
    assert resp.status_code == 204
    db.refresh(art)
    assert art.total_display_time == 0  # untouched — no DB write


def test_heartbeat_still_writes_when_demo_off(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", False)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    from models import ArtworkModel
    art = ArtworkModel(filename="x.jpg", status="approved", total_display_time=0)
    db.add(art); db.commit(); db.refresh(art)

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as c:
            resp = c.post("/api/telemetry/heartbeat",
                           json={"artwork_id": art.id, "display_time_sec": 30, "skipped": False})
        assert resp.status_code == 200
        db.refresh(art)
        assert art.total_display_time == 30
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_api_demo_reports_on(demo_client):
    body = demo_client.get("/api/demo").json()
    assert body == {
        "demo": True,
        "repo_url": "https://github.com/pieria-art/Pieria",
        "releases_url": "https://github.com/pieria-art/Pieria/releases/latest",
    }


def test_api_demo_reports_off(client):
    assert client.get("/api/demo").json() == {"demo": False}


def test_normalize_display_id():
    assert core_demo.normalize_display_id("phone-123") == "phone-123"


def test_normalize_display_id_collapses_in_demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    assert core_demo.normalize_display_id("phone-123") == "demo"
    assert core_demo.normalize_display_id("anything-else") == "demo"


def test_display_id_normalized_end_to_end(db_client):
    """Two different display ids given to the preferred-playlist route both resolve against the same
    'demo' row/setting server-side — one shared shuffle bag, not one per visitor."""
    demo, db = db_client
    from core.settings_util import _upsert_setting
    from models import ArtworkModel

    pl = PlaylistModel(name="Masterpieces")
    art = ArtworkModel(filename="x.jpg", status="approved")
    db.add_all([pl, art]); db.commit(); db.refresh(pl); db.refresh(art)
    pl.artworks.append(art); db.commit()

    _upsert_setting(db, "last_playlist:demo", "Masterpieces")
    db.commit()
    r1 = demo.get("/api/displays/visitor-a/preferred-playlist")
    r2 = demo.get("/api/displays/visitor-b/preferred-playlist")
    assert r1.json() == r2.json() == {"playlist": "Masterpieces"}


def test_ws_cap_refuses_over_limit(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.setattr(config, "DEMO_WS_MAX", 1)
    with TestClient(app) as c:
        with c.websocket_connect("/ws/one") as ws1:
            # A second concurrent connection should be refused (policy: 1013 "try again later").
            with pytest.raises(Exception):
                with c.websocket_connect("/ws/two"):
                    pass
            ws1.close()


def test_ws_denied_for_non_ws_path(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    with TestClient(app) as c:
        with pytest.raises(Exception):
            with c.websocket_connect("/not-a-real-ws-path"):
                pass


@pytest.mark.asyncio
async def test_install_demo_packs_skips_already_installed(monkeypatch):
    """SD_DEMO_PACKS only installs what's missing — mocks the registry install so no network happens."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr("database.SessionLocal", lambda: sessionmaker(bind=engine)())

    db = sessionmaker(bind=engine)()
    db.add(SubscriptionModel(url="pack:masterpieces", title="Masterpieces"))
    db.commit()
    db.close()

    calls = []

    async def _fake_install(db, client, registry_url, cid):
        calls.append(cid)
        return {"ok": True}

    class _FakeClient:
        async def aclose(self):
            pass

    monkeypatch.setattr("core.pack_fetch.install_collection_from_registry", _fake_install)
    monkeypatch.setattr("core.pack_fetch.new_client", lambda: _FakeClient())

    await core_demo.install_demo_packs(["masterpieces", "impressionism"])
    assert calls == ["impressionism"]  # masterpieces already installed -> skipped


@pytest.mark.asyncio
async def test_install_demo_packs_sets_default_playlist(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr("database.SessionLocal", lambda: sessionmaker(bind=engine)())

    class _FakeClient:
        async def aclose(self):
            pass

    async def _fake_install(db, client, registry_url, cid):
        return {"ok": True}

    monkeypatch.setattr("core.pack_fetch.install_collection_from_registry", _fake_install)
    monkeypatch.setattr("core.pack_fetch.new_client", lambda: _FakeClient())

    await core_demo.install_demo_packs(["masterpieces"], default_playlist="Masterpieces")

    check = sessionmaker(bind=engine)()
    from models import SettingsModel
    row = check.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    assert row is not None and row.setting_value == "Masterpieces"
    check.close()
