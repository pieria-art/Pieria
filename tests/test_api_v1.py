"""Public API v1 (ADR-147): token lifecycle, scopes, every endpoint, and the never-advance guarantee."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
from app import app
from core import api_tokens
from database import Base, get_db
from models import (
    ActiveDisplayModel,
    ApiTokenModel,
    ArtworkModel,
    DisplayPlaybackSessionModel,
    PlaylistModel,
    RemoteCommandModel,
    SettingsModel,
    playlist_artwork,
)
from routers.api_v1 import v1_app

ADMIN = {"Origin": "http://testserver"}   # what the same-origin admin page sends


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override():
        yield db
    app.dependency_overrides[get_db] = _override
    v1_app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c, db
    app.dependency_overrides.clear()
    v1_app.dependency_overrides.clear()
    db.close()


def _mint(db, scopes=("read", "control"), name="t"):
    _, plaintext = api_tokens.mint_token(db, name, scopes)
    return {"Authorization": f"Bearer {plaintext}"}


def _art(db, title="Starry Night", artist="van Gogh", **kw):
    a = ArtworkModel(filename=f"{title}.jpg", status="approved", title=title, agent_name=artist, **kw)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _playlist(db, name, arts, **kw):
    p = PlaylistModel(name=name, **kw)
    db.add(p); db.commit(); db.refresh(p)
    for i, a in enumerate(arts):
        db.execute(playlist_artwork.insert().values(playlist_id=p.id, artwork_id=a.id, display_order=i))
    db.commit()
    return p


def _display(db, display_id="wall", age_s=1, **kw):
    d = ActiveDisplayModel(display_id=display_id,
                           last_seen_at=datetime.now(UTC) - timedelta(seconds=age_s), **kw)
    db.add(d); db.commit()
    return d


# --- tokens ----------------------------------------------------------------------------------------

def test_token_lifecycle_plaintext_once_then_revoke(env):
    c, db = env
    r = c.post("/api/settings/api-tokens", json={"name": "HA", "scopes": ["read"]}, headers=ADMIN)
    assert r.status_code == 201
    body = r.json()
    token = body["token"]
    assert token.startswith("pieria_") and len(token) >= 7 + 43   # 32 bytes urlsafe
    assert body["scopes"] == ["read"] and body["last_used_at"] is None

    listed = c.get("/api/settings/api-tokens", headers={"Sec-Fetch-Site": "same-origin"}).json()
    assert [t["name"] for t in listed] == ["HA"]
    assert token not in json.dumps(listed) and "token" not in listed[0]

    # Only the hash is at rest.
    row = db.query(ApiTokenModel).one()
    assert row.token_hash == api_tokens.hash_token(token) and token not in (row.token_hash, row.name)

    auth = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/v1/info", headers=auth).status_code == 200
    assert c.delete(f"/api/settings/api-tokens/{body['id']}", headers=ADMIN).status_code == 200
    r = c.get("/api/v1/info", headers=auth)
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    # revoked tokens stay listed, flagged
    assert c.get("/api/settings/api-tokens", headers=ADMIN).json()[0]["revoked_at"] is not None


def test_mint_validation_and_unknown_revoke(env):
    c, _ = env
    assert c.post("/api/settings/api-tokens", json={"name": "x", "scopes": ["root"]}, headers=ADMIN).status_code == 400
    assert c.post("/api/settings/api-tokens", json={"name": "x", "scopes": []}, headers=ADMIN).status_code == 400
    assert c.post("/api/settings/api-tokens", json={"name": "  ", "scopes": ["read"]}, headers=ADMIN).status_code == 400
    assert c.delete("/api/settings/api-tokens/999", headers=ADMIN).status_code == 404


def test_management_routes_refuse_a_plain_lan_caller(env, monkeypatch):
    """No Origin, no Sec-Fetch-Site, no token configured (plain docker-compose): refused, fail closed."""
    c, _ = env
    monkeypatch.setattr(config, "APPLIANCE_UPDATE_TOKEN", "")
    assert c.get("/api/settings/api-tokens").status_code == 403
    assert c.post("/api/settings/api-tokens", json={"name": "x", "scopes": ["read"]}).status_code == 403
    assert c.delete("/api/settings/api-tokens/1").status_code == 403
    # ...and a hostile cross-origin tab is refused too
    assert c.post("/api/settings/api-tokens", json={"name": "x", "scopes": ["read"]},
                  headers={"Origin": "http://evil.example"}).status_code == 403


def test_management_accepts_the_appliance_token(env, monkeypatch):
    c, _ = env
    monkeypatch.setattr(config, "APPLIANCE_UPDATE_TOKEN", "s3cret")
    r = c.post("/api/settings/api-tokens", json={"name": "x", "scopes": ["read"]},
               headers={"X-Appliance-Token": "s3cret"})
    assert r.status_code == 201


def test_last_used_moves_at_most_once_a_minute(env):
    c, db = env
    h = _mint(db)
    c.get("/api/v1/info", headers=h)
    first = db.query(ApiTokenModel).one().last_used_at
    assert first is not None
    c.get("/api/v1/info", headers=h)
    assert db.query(ApiTokenModel).one().last_used_at == first
    row = db.query(ApiTokenModel).one()
    row.last_used_at = datetime.now(UTC) - timedelta(minutes=2); db.commit()
    c.get("/api/v1/info", headers=h)
    assert db.query(ApiTokenModel).one().last_used_at > first - timedelta(minutes=1)


# --- auth + scopes -----------------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/info"), ("get", "/api/v1/displays"), ("get", "/api/v1/displays/wall"),
    ("post", "/api/v1/displays/wall/commands"), ("get", "/api/v1/playlists"),
    ("get", "/api/v1/playlists/1/artworks"), ("get", "/api/v1/artworks/1"), ("get", "/api/v1/search?q=a"),
    ("get", "/api/v1/schedule"), ("patch", "/api/v1/schedule"), ("get", "/api/v1/schedule/state"),
])
def test_every_route_requires_a_token(env, method, path):
    c, _ = env
    r = getattr(c, method)(path)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert r.json() == {"error": {"code": "unauthorized", "message": "missing bearer token"}}
    r = getattr(c, method)(path, headers={"Authorization": "Bearer pieria_nope"})
    assert r.status_code == 401
    r = getattr(c, method)(path, headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert r.status_code == 401


def test_scope_enforcement(env):
    c, db = env
    _art(db); _display(db)
    read, control = _mint(db, ["read"]), _mint(db, ["control"])
    assert c.get("/api/v1/displays", headers=read).status_code == 200
    assert c.get("/api/v1/displays", headers=control).status_code == 403
    r = c.post("/api/v1/displays/wall/commands", json={"action": "next"}, headers=read)
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"
    assert c.post("/api/v1/displays/wall/commands", json={"action": "next"}, headers=control).status_code == 202
    assert c.patch("/api/v1/schedule", json={"enabled": True}, headers=read).status_code == 403
    assert c.patch("/api/v1/schedule", json={"enabled": True}, headers=control).status_code == 200


def test_mounted_app_keeps_the_global_guards(env):
    c, db = env
    h = _mint(db)
    r = c.get("/api/v1/info", headers=h)
    assert "no-store" in r.headers["cache-control"]                 # CacheHeadersMiddleware
    assert r.headers["x-content-type-options"] == "nosniff"
    r = c.post("/api/v1/displays/wall/commands", json={"action": "next"},
               headers={**h, "Origin": "http://evil.example"})
    assert r.status_code == 403                                      # CorsAndOriginGuardMiddleware


def test_openapi_is_v1_only(env):
    c, _ = env
    spec = c.get("/api/v1/openapi.json").json()
    assert spec["info"]["title"] == "Pieria API" and spec["info"]["version"] == "1"
    assert all(p.startswith("/") and "/api/" not in p for p in spec["paths"])
    assert "/info" in spec["paths"] and "/displays/{display_id}/commands" in spec["paths"]
    assert "BearerAuth" in spec["components"]["securitySchemes"]
    assert c.get("/api/v1/docs").status_code == 200
    assert "/api/v1/info" not in c.get("/openapi.json").json()["paths"]


# --- info / displays ---------------------------------------------------------------------------------

def test_info_server_id_stable(env):
    c, db = env
    h = _mint(db)
    a = c.get("/api/v1/info", headers=h).json()
    b = c.get("/api/v1/info", headers=h).json()
    assert a["name"] == "Pieria" and a["api_version"] == 1 and a["version"] == config.APP_VERSION
    assert isinstance(a["appliance_mode"], bool) and a["display_name"] is None
    assert a["server_id"] == b["server_id"]
    assert db.query(SettingsModel).filter_by(setting_key="server_id").one().setting_value == a["server_id"]


def test_displays_shape_and_kinds(env):
    c, db = env
    art = _art(db, date_display="1889")
    _playlist(db, "Summer", [art], default_mode="static-crop")
    _display(db, "wall", kind="canvas", current_artwork_id=art.id, current_playlist="Summer")
    _display(db, "panel", kind="eink")
    _display(db, "frame-tv")
    _display(db, "mystery")
    h = _mint(db, ["read"])
    got = {d["id"]: d for d in c.get("/api/v1/displays", headers=h).json()}
    assert {k: v["kind"] for k, v in got.items()} == {"wall": "canvas", "panel": "eink",
                                                       "frame-tv": "frame", "mystery": "unknown"}
    w = got["wall"]
    assert w["live"] is True and w["playlist"] == "Summer" and w["mode"] == "static-crop"
    assert w["last_seen"].endswith("Z") or "+00:00" in w["last_seen"]
    assert w["artwork"] == {"id": art.id, "title": "Starry Night", "artist": "van Gogh", "year": "1889",
                            "image_url": f"/artworks/{art.id}/display.jpg",
                            "thumb_url": f"/artworks/{art.id}/thumbnail", "is_personal": False}
    assert got["panel"]["artwork"] is None and got["panel"]["mode"] is None
    one = c.get("/api/v1/displays/wall", headers=h).json()
    assert one == w
    assert c.get("/api/v1/displays/nope", headers=h).status_code == 404
    assert c.get("/api/v1/displays/nope", headers=h).json()["error"]["code"] == "not_found"


def test_displays_reads_never_advance_playback(env):
    c, db = env
    arts = [_art(db, f"A{i}") for i in range(3)]
    _playlist(db, "Seq", arts)
    _display(db, "wall", kind="canvas", current_artwork_id=arts[0].id, current_playlist="Seq")
    h = _mint(db, ["read"])
    for _ in range(3):
        c.get("/api/v1/displays", headers=h)
        c.get("/api/v1/displays/wall", headers=h)
    row = db.query(ActiveDisplayModel).one()
    assert row.current_artwork_id == arts[0].id and row.current_playlist == "Seq"
    assert db.query(DisplayPlaybackSessionModel).count() == 0
    assert db.query(RemoteCommandModel).count() == 0


# --- commands ----------------------------------------------------------------------------------------

def test_commands_queue_in_the_internal_shape(env):
    c, db = env
    _playlist(db, "Summer", [_art(db)])
    _display(db, "wall", kind="canvas")
    h = _mint(db, ["control"])
    cases = [({"action": "next"}, {"action": "next_image"}),
             ({"action": "previous"}, {"action": "prev_image"}),
             ({"action": "show_placard"}, {"action": "show_placard"}),
             ({"action": "set_playlist", "playlist": "Summer"}, {"action": "set_playlist", "playlist": "Summer"}),
             ({"action": "set_mode", "mode": "contain-matte"}, {"action": "set_mode", "mode": "contain-matte"})]
    for body, internal in cases:
        r = c.post("/api/v1/displays/wall/commands", json=body, headers=h)
        assert r.status_code == 202 and r.json() == {"status": "queued"}
        cmd = db.query(RemoteCommandModel).order_by(RemoteCommandModel.id.desc()).first()
        assert cmd.target_display == "wall" and json.loads(cmd.payload) == internal
    assert db.query(RemoteCommandModel).count() == len(cases)
    # the commanded mode is what /displays then reports for a canvas
    read = _mint(db, ["read"])
    assert c.get("/api/v1/displays/wall", headers=read).json()["mode"] == "contain-matte"


def test_command_to_non_live_display_is_409_and_not_queued(env):
    c, db = env
    _display(db, "asleep", age_s=120, kind="canvas")
    h = _mint(db, ["control"])
    r = c.post("/api/v1/displays/asleep/commands", json={"action": "next"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_live"
    assert db.query(RemoteCommandModel).count() == 0
    assert c.post("/api/v1/displays/ghost/commands", json={"action": "next"}, headers=h).status_code == 404


@pytest.mark.parametrize("body", [
    {"action": "explode"}, {"action": "pause"},                       # unknown / not-yet-added (A2)
    {"action": "set_mode"}, {"action": "set_mode", "mode": "disco"},
    {"action": "set_playlist"}, {"action": "next", "bogus": 1}, {},
])
def test_command_bad_body_is_422(env, body):
    c, db = env
    _display(db, "wall")
    r = c.post("/api/v1/displays/wall/commands", json=body, headers=_mint(db, ["control"]))
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert db.query(RemoteCommandModel).count() == 0


def test_set_playlist_must_exist_and_hides_internal(env):
    c, db = env
    _playlist(db, "_derivatives", [])
    _display(db, "wall")
    h = _mint(db, ["control"])
    for name in ("Nope", "_derivatives"):
        r = c.post("/api/v1/displays/wall/commands", json={"action": "set_playlist", "playlist": name}, headers=h)
        assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_playlist"
    assert db.query(RemoteCommandModel).count() == 0


# --- library -----------------------------------------------------------------------------------------

def test_playlists_light_and_paged_artworks(env):
    c, db = env
    arts = [_art(db, f"Work {i}") for i in range(5)]
    pending = ArtworkModel(filename="p.jpg", status="pending_review", title="Hidden")
    db.add(pending); db.commit()
    p = _playlist(db, "Summer", arts + [pending], display_time=45, shuffle=True)
    _playlist(db, "_derivatives", [])
    h = _mint(db, ["read"])
    lst = c.get("/api/v1/playlists", headers=h).json()
    assert lst == [{"id": p.id, "name": "Summer", "artwork_count": 5, "display_time": 45, "shuffle": True,
                    "default_mode": "ken-burns", "is_personal": False}]
    page = c.get(f"/api/v1/playlists/{p.id}/artworks?limit=2&offset=1", headers=h).json()
    assert page["total"] == 5 and page["limit"] == 2 and page["offset"] == 1
    assert [a["title"] for a in page["items"]] == ["Work 1", "Work 2"]
    assert c.get(f"/api/v1/playlists/{p.id}/artworks?limit=0", headers=h).status_code == 422
    assert c.get("/api/v1/playlists/999/artworks", headers=h).status_code == 404
    hidden = db.query(PlaylistModel).filter_by(name="_derivatives").one()
    assert c.get(f"/api/v1/playlists/{hidden.id}/artworks", headers=h).status_code == 404


def test_artwork_detail_with_licence_and_safe_hrefs(env):
    c, db = env
    a = _art(db, title="**Bold** Title", description_narrative="A *calm* scene.", tags="calm, blue ,",
             cultural_context="Impressionism", medium="Oil", series="Series X", license="CC-BY-4.0",
             license_url="https://creativecommons.org/licenses/by/4.0/", attribution="ESA/Webb",
             attribution_url="javascript:alert(1)", origin_url="https://example.org/work/1")
    pending = ArtworkModel(filename="p.jpg", status="pending_review")
    db.add(pending); db.commit()
    h = _mint(db, ["read"])
    d = c.get(f"/api/v1/artworks/{a.id}", headers=h).json()
    assert d["title"] == "Bold Title" and d["placard"] == "A calm scene."
    assert d["tags"] == ["calm", "blue"] and d["culture"] == "Impressionism" and d["series"] == "Series X"
    assert d["license"]["id"] == "CC-BY-4.0" and d["license"]["requires_attribution"] is True
    assert d["license"]["url"].startswith("https://") and d["license"]["attribution"] == "ESA/Webb"
    assert d["license"]["attribution_url"] is None                       # javascript: refused
    assert d["license"]["origin_url"] == "https://example.org/work/1"
    assert d["image_url"] == f"/artworks/{a.id}/display.jpg"
    assert c.get("/api/v1/artworks/999", headers=h).status_code == 404
    assert c.get(f"/api/v1/artworks/{pending.id}", headers=h).status_code == 404


def test_search_tokenized_and_case_insensitive(env):
    c, db = env
    monet = _art(db, "Water Lilies", "Claude Monet", cultural_context="Impressionism")
    _art(db, "Haystacks", "Claude Monet", description_narrative="A calm field at dusk.")
    _art(db, "Calm Sea", "Hiroshige")
    _art(db, "Storm", "Turner", description_narrative="Not at all peaceful.")
    pending = ArtworkModel(filename="x.jpg", status="pending_review", title="Calm Pending")
    db.add(pending); db.commit()
    h = _mint(db, ["read"])

    def q(s, **kw):
        r = c.get("/api/v1/search", params={"q": s, **kw}, headers=h)
        assert r.status_code == 200
        return r.json()

    assert [i["title"] for i in q("MONET")["items"]] == ["Water Lilies", "Haystacks"]
    assert [i["title"] for i in q("monet impressionism")["items"]] == ["Water Lilies"]   # AND across fields
    calm = q("calm")
    assert calm["total"] == 2 and calm["items"][0]["title"] == "Calm Sea"               # title hit ranks first
    assert "Calm Pending" not in [i["title"] for i in calm["items"]]                      # approved only
    assert q("calm", limit=1)["total"] == 2 and len(q("calm", limit=1)["items"]) == 1
    assert q("100%")["total"] == 0 and q("a_b")["total"] == 0                             # LIKE wildcards literal
    assert q("zzzz")["items"] == []
    assert c.get("/api/v1/search", params={"q": ""}, headers=h).status_code == 422
    assert c.get("/api/v1/search", headers=h).status_code == 422
    assert monet.id == q("lilies")["items"][0]["id"]


# --- schedule ----------------------------------------------------------------------------------------

def test_schedule_get_patch_state(env):
    c, db = env
    read, ctl = _mint(db, ["read"]), _mint(db, ["control", "read"])
    s = c.get("/api/v1/schedule", headers=read).json()
    assert s["quiet_enabled"] is False and s["quiet_mode"] == "cec" and s["day_start"] == "08:00"
    r = c.patch("/api/v1/schedule", json={"quiet_enabled": True, "quiet_start": "00:00", "quiet_end": "23:59",
                                          "night_brightness": 0.5}, headers=ctl)
    assert r.status_code == 200 and r.json()["quiet_enabled"] is True and r.json()["night_brightness"] == 0.5
    assert c.get("/api/v1/schedule", headers=read).json() == r.json()
    # persisted through the SAME key the internal route + Canvas read
    assert c.get("/api/settings/display-schedule").json() == r.json()
    st = c.get("/api/v1/schedule/state", headers=read).json()
    assert set(st) == {"enabled", "brightness", "warmth", "quiet", "quiet_mode"} and st["quiet"] is True


@pytest.mark.parametrize("body", [{"day_brightness": 5}, {"quiet_start": "late"}, {"night_warmth": -1},
                                  {"quiet_mode": "nuke"}, {"unknown": 1}])
def test_schedule_patch_validation_is_422_and_writes_nothing(env, body):
    c, db = env
    r = c.patch("/api/v1/schedule", json=body, headers=_mint(db, ["control"]))
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert db.query(SettingsModel).filter_by(setting_key="display_schedule").count() == 0


def test_internal_schedule_route_still_returns_400(env):
    """The validator moved into core.settings_util; the internal route's contract did not change."""
    c, _ = env
    r = c.post("/api/settings/display-schedule", json={"day_brightness": 5})
    assert r.status_code == 400 and "day_brightness" in r.json()["detail"]


# --- kind stamping ----------------------------------------------------------------------------------

def test_touch_active_display_stamps_eink_kind(env):
    from core.playback import touch_active_display
    _, db = env
    touch_active_display(db, "panel", refresh_s=900)
    assert db.query(ActiveDisplayModel).one().kind == "eink"
