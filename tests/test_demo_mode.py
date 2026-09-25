"""Public demo mode (SD_DEMO_MODE=1, core/demo.py) — the default-DENY gate, display-id
normalization (and its no-persist guarantee), /api/demo, and SD_DEMO_PACKS bootstrap.

WebSocket: demo mode refuses EVERY /ws/{id} connection outright (no allowlist entry, no cap needed —
see core/demo.py's DemoModeMiddleware). See test_ws_always_refused_in_demo below."""

import fastapi.routing as fastapi_routing
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

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


# --- The ground truth for the sweep below, authored independently of core.demo.ALLOWED_ROUTES ------
# A previous version of this test asked core_demo._is_allowed_http/_is_static_asset what SHOULD be
# allowed and then checked the gate agrees with itself — a bypass in that predicate would pass its own
# test. This hardcodes the expected (method, path-template) set by hand, exactly as FastAPI's own route
# objects spell the templates (see _enumerate_routes), so the sweep is an independent check against the
# real app.routes, not a tautology.
_EXPECTED_ALLOWED = {
    ("GET", "/admin"),
    ("GET", "/help"),
    ("GET", "/art/{artwork_id}"),
    ("GET", "/next-image"),
    ("GET", "/artworks/{artwork_id}/display.jpg"),
    ("GET", "/api/displays/{display_id}/preferred-playlist"),
    ("GET", "/api/displays/{display_id}/schedule-state"),
    ("GET", "/api/displays/{display_id}/now-playing"),
    ("GET", "/playlists"),
    ("GET", "/artworks"),
    ("GET", "/artworks/{artwork_id}/thumbnail"),
    ("GET", "/artworks/{artwork_id}/preview"),
    ("GET", "/artworks/{artwork_id}/placard"),
    ("GET", "/api/catalog"),
    ("GET", "/api/catalog/{collection_id}"),
    ("GET", "/api/packs"),
    ("GET", "/api/packs/status"),
    ("GET", "/api/remote/displays"),
    ("GET", "/api/demo"),
    ("POST", "/api/telemetry/heartbeat"),
}


def test_default_deny_sweep(demo_client):
    """Every app route NOT in the hardcoded _EXPECTED_ALLOWED set must 403 in demo mode; every route
    IN it must NOT 403 — the guard against a new route landing without an allowlist decision, checked
    against an independently authored ground truth rather than the gate's own predicate. WS routes are
    refused unconditionally now (no allowlist entry at all) and are covered by a dedicated test."""
    for methods, path in _enumerate_routes():
        if methods == ("WS",):
            continue
        concrete = _concretize(path)
        for method in methods:
            resp = demo_client.request(method, concrete)
            if (method, path) in _EXPECTED_ALLOWED:
                assert resp.status_code != 403, f"{method} {path} unexpectedly gated"
            else:
                assert resp.status_code == 403, f"{method} {path} should be demo-denied, got {resp.status_code}"
                assert "install your own" in resp.json()["detail"]


def test_expected_allowed_set_matches_every_real_route():
    """A route template in _EXPECTED_ALLOWED that doesn't exist in the app any more (renamed/removed)
    would silently stop being exercised by the sweep above — catch that drift explicitly."""
    real = {(m, p) for methods, p in _enumerate_routes() for m in methods if methods != ("WS",)}
    missing = _EXPECTED_ALLOWED - real
    assert not missing, f"_EXPECTED_ALLOWED names route(s) that don't exist: {missing}"


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


@pytest.mark.parametrize("path", [
    # httpx (TestClient's transport) normalizes '..'/'.' segments per RFC 3986 BEFORE sending — so at
    # this level these arrive as e.g. "/studio.html", not the literal dotdot form. Still worth a sanity
    # check that the normalized target is independently denied (top-level *.html isn't in
    # core.demo._STATIC_FILES). The real regression test for the literal-dotdot bypass is below, driven
    # straight at the ASGI middleware — a raw socket / non-normalizing client DOES send the literal form.
    "/catalog/../studio.html",
    "/vendor/../studio.html",
])
def test_dotdot_path_client_normalized_form_still_denied(demo_client, path):
    resp = demo_client.get(path)
    assert resp.status_code == 403


def test_has_dotdot_segment_unit():
    assert core_demo._has_dotdot_segment("/catalog/../studio.html")
    assert core_demo._has_dotdot_segment("/./admin")
    assert core_demo._has_dotdot_segment("/../admin")
    assert not core_demo._has_dotdot_segment("/catalog/index.json")
    assert not core_demo._has_dotdot_segment("/admin")


def _drive_middleware_http(path: str, method: str = "GET") -> int:
    """Call core.demo.DemoModeMiddleware directly with a hand-built ASGI scope carrying a LITERAL
    dot-segment path. httpx (what TestClient uses) normalizes '..'/'.' client-side before a request
    ever goes out — see test_dotdot_path_client_normalized_form_still_denied's docstring — so an
    httpx-driven test can never actually deliver "/catalog/../studio.html" to our app; a raw socket
    client (curl, or anything not doing RFC 3986 normalization) does. This is what really exercises
    the guard against the bypass a reviewer found."""
    import asyncio

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def dummy_downstream(scope, receive, send):
        # Would only run if the middleware wrongly let the request through.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"should never be reached"})

    mw = core_demo.DemoModeMiddleware(dummy_downstream)
    scope = {"type": "http", "method": method, "path": path, "headers": []}
    asyncio.run(mw(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"]


@pytest.mark.parametrize("path", [
    # "catalog"/"vendor"/"docs" are all allowed static DIRECTORIES — a naive check that only looks at
    # the first path segment would let any of these through, even though the literal target resolves,
    # once StaticFiles serves it, to the denied static/studio.html (the SPA shell behind /studio).
    "/catalog/../studio.html",
    "/catalog/./../studio.html",
    "/vendor/../studio.html",
    "/docs/../studio.html",
    # A dot segment on an otherwise-fully-allowed path — no legitimate client ever sends one.
    "/./admin",
    "/../admin",
])
def test_literal_dotdot_path_denied_at_the_middleware(monkeypatch, path):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    assert _drive_middleware_http(path) == 403


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
    """In demo mode the 'last played' lookup is skipped entirely (core/demo.py, routers/display.py) —
    so even a pre-existing last_playlist:demo row (e.g. a stale write from before this guard existed)
    must be IGNORED, and the global default must win for every display id, always. This is what stops
    one visitor's gallery switch from silently overriding SD_DEMO_DEFAULT_PLAYLIST for everyone else."""
    demo, db = db_client
    from core.settings_util import _upsert_setting
    from models import ArtworkModel

    default_pl = PlaylistModel(name="Masterpieces")
    other_pl = PlaylistModel(name="SomethingElse")
    art1 = ArtworkModel(filename="x.jpg", status="approved")
    art2 = ArtworkModel(filename="y.jpg", status="approved")
    db.add_all([default_pl, other_pl, art1, art2]); db.commit()
    db.refresh(default_pl); db.refresh(other_pl); db.refresh(art1); db.refresh(art2)
    default_pl.artworks.append(art1)
    other_pl.artworks.append(art2)
    db.commit()

    _upsert_setting(db, "default_playlist", "Masterpieces")
    _upsert_setting(db, "last_playlist:demo", "SomethingElse")  # must be ignored
    db.commit()

    r1 = demo.get("/api/displays/visitor-a/preferred-playlist")
    r2 = demo.get("/api/displays/visitor-b/preferred-playlist")
    assert r1.json() == r2.json() == {"playlist": "Masterpieces"}


def test_next_image_does_not_persist_last_playlist_in_demo(db_client):
    """core/playback.py's select_next_image must not write last_playlist:<id> in demo mode — that
    write is exactly what would let one visitor's gallery switch override the default for everyone
    (the read side of the same guarantee is test_display_id_normalized_end_to_end above)."""
    demo, db = db_client
    from models import ArtworkModel, SettingsModel

    pl = PlaylistModel(name="Masterpieces")
    art = ArtworkModel(filename="x.jpg", status="approved")
    db.add_all([pl, art]); db.commit(); db.refresh(pl); db.refresh(art)
    pl.artworks.append(art); db.commit()

    resp = demo.get("/next-image", params={"playlist_name": "Masterpieces", "display_id": "visitor-a"})
    assert resp.status_code == 200
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == "last_playlist:demo").first()
    assert row is None


def test_next_image_still_persists_last_playlist_when_demo_off(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", False)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    from models import ArtworkModel, SettingsModel

    pl = PlaylistModel(name="Masterpieces")
    art = ArtworkModel(filename="x.jpg", status="approved")
    db.add_all([pl, art]); db.commit(); db.refresh(pl); db.refresh(art)
    pl.artworks.append(art); db.commit()

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as c:
            resp = c.get("/next-image", params={"playlist_name": "Masterpieces", "display_id": "phone-1"})
        assert resp.status_code == 200
        row = db.query(SettingsModel).filter(SettingsModel.setting_key == "last_playlist:phone-1").first()
        assert row is not None and row.setting_value == "Masterpieces"
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_ws_always_refused_in_demo(monkeypatch):
    """No WebSocket surface in demo mode at all — not even the real /ws/{id} path (a visitor's socket
    cost a SQLite commit per heartbeat frame plus its own 1 Hz poller task; the display doesn't need
    it either, see core/demo.py). Closed before accept -> the client sees a disconnect, never a session."""
    monkeypatch.setattr(config, "DEMO_MODE", True)
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/some-display"):
                pass


def test_ws_allowed_when_demo_off(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", False)
    with TestClient(app) as c:
        with c.websocket_connect("/ws/some-display") as ws:
            ws.close()


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
