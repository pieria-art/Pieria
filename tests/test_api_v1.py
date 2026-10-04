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
    ("post", "/api/v1/displays/wall/show"), ("get", "/api/v1/quiet"), ("post", "/api/v1/quiet"),
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
    {"action": "explode"}, {"action": "Pause"},                       # unknown / wrong-case
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


# --- A2: pause / resume ------------------------------------------------------------------------------

def _next(c, display="wall", playlist="Seq", **params):
    q = {"playlist_name": playlist, "display_id": display, **params}
    r = c.get("/next-image", params=q)
    assert r.status_code == 200, r.text
    return r.json()


def test_pause_holds_next_image_and_explicit_next_advances_once(env):
    c, db = env
    arts = [_art(db, f"A{i}") for i in range(4)]
    _playlist(db, "Seq", arts)
    _display(db, "wall", kind="canvas")
    ctl, read = _mint(db, ["control"]), _mint(db, ["read"])
    first = _next(c)["metadata"]["id"]
    assert first == arts[0].id and _next(c)["metadata"]["id"] == arts[1].id   # unpaused: advances

    r = c.post("/api/v1/displays/wall/commands", json={"action": "pause"}, headers=ctl)
    assert r.status_code == 202 and r.json() == {"status": "queued"}
    assert c.get("/api/v1/displays/wall", headers=read).json()["paused"] is True
    held = [_next(c) for _ in range(3)]                                        # auto-advance timer ticks
    assert {h["metadata"]["id"] for h in held} == {arts[1].id} and all(h["paused"] for h in held)

    assert _next(c, manual="true")["metadata"]["id"] == arts[2].id            # explicit next: exactly once
    assert _next(c)["metadata"]["id"] == arts[2].id                            # ... and it stays paused
    assert _next(c, direction=-1)["metadata"]["id"] == arts[1].id              # previous also advances once
    assert _next(c)["metadata"]["id"] == arts[1].id

    assert c.post("/api/v1/displays/wall/commands", json={"action": "resume"}, headers=ctl).status_code == 202
    assert c.get("/api/v1/displays/wall", headers=read).json()["paused"] is False
    nxt = _next(c)
    assert nxt["metadata"]["id"] == arts[2].id and nxt["paused"] is False
    # the relay got the Canvas-facing commands
    acts = [json.loads(m.payload)["action"] for m in db.query(RemoteCommandModel).order_by(RemoteCommandModel.id)]
    assert acts == ["pause", "resume"]


def test_pause_resume_on_sleeping_eink_sets_flag_without_queueing(env):
    c, db = env
    _display(db, "panel", age_s=3600, kind="eink")
    _display(db, "cv", age_s=3600, kind="canvas")
    ctl, read = _mint(db, ["control"]), _mint(db, ["read"])
    r = c.post("/api/v1/displays/panel/commands", json={"action": "pause"}, headers=ctl)
    assert r.status_code == 202
    assert c.get("/api/v1/displays/panel", headers=read).json()["paused"] is True
    assert db.query(RemoteCommandModel).count() == 0                 # nothing rots in the queue
    assert c.post("/api/v1/displays/panel/commands", json={"action": "resume"}, headers=ctl).status_code == 202
    assert c.get("/api/v1/displays/panel", headers=read).json()["paused"] is False
    # canvas-only actions + a sleeping canvas still 409, pause included
    assert c.post("/api/v1/displays/panel/commands", json={"action": "next"}, headers=ctl).status_code == 409
    r = c.post("/api/v1/displays/cv/commands", json={"action": "pause"}, headers=ctl)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_live"
    assert c.get("/api/v1/displays/cv", headers=read).json()["paused"] is False
    assert c.post("/api/v1/displays/ghost/commands", json={"action": "pause"}, headers=ctl).status_code == 404


def test_pause_flag_survives_the_display_row_being_deleted(env):
    """A Canvas reload deletes its active_displays row on WS disconnect; the pause must outlive that."""
    from core.playback import is_display_paused, set_display_paused
    _, db = env
    _display(db, "wall")
    set_display_paused(db, "wall", True)
    db.query(ActiveDisplayModel).delete()
    db.commit()
    assert is_display_paused(db, "wall") is True
    set_display_paused(db, "wall", False)
    assert is_display_paused(db, "wall") is False


# --- A2: show artwork now ----------------------------------------------------------------------------

def test_show_on_canvas_queues_show_artwork_and_next_image_serves_it(env):
    c, db = env
    arts = [_art(db, f"A{i}") for i in range(3)]
    other = _art(db, "Elsewhere")                                    # approved, in no playlist
    _playlist(db, "Seq", arts)
    _display(db, "wall", kind="canvas")
    ctl = _mint(db, ["control"])
    r = c.post("/api/v1/displays/wall/show", json={"artwork_id": other.id}, headers=ctl)
    assert r.status_code == 202 and r.json() == {"status": "queued"}
    cmd = db.query(RemoteCommandModel).one()
    assert cmd.target_display == "wall"
    assert json.loads(cmd.payload) == {"action": "show_artwork", "artwork_id": other.id}
    # what the Canvas then does: /next-image with artwork_id
    shown = _next(c, artwork_id=other.id, manual="true")
    assert shown["metadata"]["id"] == other.id and shown["index"] == -1
    row = db.query(ActiveDisplayModel).one()
    assert row.current_artwork_id == other.id and row.current_playlist == "Seq"
    # the rotation continues from the sequence, undisturbed
    assert _next(c)["metadata"]["id"] == arts[0].id
    assert c.get("/next-image", params={"playlist_name": "Seq", "display_id": "wall",
                                        "artwork_id": 99999}).status_code == 404


def test_show_errors(env):
    c, db = env
    art = _art(db)
    pending = ArtworkModel(filename="p.jpg", status="pending", title="p")
    db.add(pending)
    db.commit()
    db.refresh(pending)
    _display(db, "wall", kind="canvas")
    _display(db, "asleep", age_s=120, kind="canvas")
    ctl = _mint(db, ["control"])
    assert c.post("/api/v1/displays/ghost/show", json={"artwork_id": art.id}, headers=ctl).status_code == 404
    assert c.post("/api/v1/displays/wall/show", json={"artwork_id": 9999}, headers=ctl).status_code == 404
    assert c.post("/api/v1/displays/wall/show", json={"artwork_id": pending.id}, headers=ctl).status_code == 404
    r = c.post("/api/v1/displays/asleep/show", json={"artwork_id": art.id}, headers=ctl)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_live"
    for body in ({}, {"artwork_id": "x"}, {"artwork_id": art.id, "x": 1}):
        assert c.post("/api/v1/displays/wall/show", json=body, headers=ctl).status_code == 422
    assert db.query(RemoteCommandModel).count() == 0


def test_show_on_sleeping_eink_is_served_on_next_pull_once(env):
    c, db = env
    arts = [_art(db, f"A{i}") for i in range(3)]
    other = _art(db, "Special")
    _playlist(db, "Seq", arts)
    _display(db, "panel", age_s=3600, kind="eink")
    ctl = _mint(db, ["control"])
    r = c.post("/api/v1/displays/panel/show", json={"artwork_id": other.id}, headers=ctl)
    assert r.status_code == 202
    assert db.query(RemoteCommandModel).count() == 0
    assert _next(c, "panel")["metadata"]["id"] == other.id           # the pull (auto-advance path)
    assert _next(c, "panel")["metadata"]["id"] == arts[0].id         # one-shot: back to the rotation


def test_show_while_paused_serves_it_then_holds_it(env):
    c, db = env
    arts = [_art(db, f"A{i}") for i in range(3)]
    other = _art(db, "Special")
    _playlist(db, "Seq", arts)
    _display(db, "panel", kind="eink")
    ctl = _mint(db, ["control"])
    _next(c, "panel")
    c.post("/api/v1/displays/panel/commands", json={"action": "pause"}, headers=ctl)
    c.post("/api/v1/displays/panel/show", json={"artwork_id": other.id}, headers=ctl)
    assert _next(c, "panel")["metadata"]["id"] == other.id
    assert _next(c, "panel")["metadata"]["id"] == other.id           # paused -> holds the shown item


def test_expired_pending_show_is_ignored(env):
    from core.playback import set_pending_show
    c, db = env
    arts = [_art(db, f"A{i}") for i in range(2)]
    other = _art(db, "Special")
    _playlist(db, "Seq", arts)
    set_pending_show(db, "panel", other.id, ttl_sec=-1)
    assert _next(c, "panel")["metadata"]["id"] == arts[0].id


# --- A2: manual quiet override (tri-state: on / off / auto) --------------------------------------------

def _quiet_schedule(c, db, start, end, enabled=True):
    c.patch("/api/v1/schedule", json={"quiet_enabled": enabled, "quiet_start": start, "quiet_end": end},
            headers=_mint(db, ["control"]))


def _iso(dt):
    return dt.astimezone(UTC).isoformat()


def _expire_override(db, on):
    row = db.query(SettingsModel).filter_by(setting_key="quiet_override").one()
    row.setting_value = json.dumps({"on": on, "until": (datetime.now(UTC) - timedelta(seconds=1)).timestamp()})
    db.commit()


def test_quiet_default_and_schedule_source(env):
    c, db = env
    read = _mint(db, ["read"])
    assert c.get("/api/v1/quiet", headers=read).json() == {"active": False, "mode": "auto",
                                                           "source": "none", "until": None}
    _quiet_schedule(c, db, "00:00", "23:59")
    q = c.get("/api/v1/quiet", headers=read).json()
    assert q["active"] is True and q["mode"] == "auto" and q["source"] == "schedule"
    assert datetime.fromisoformat(q["until"]) > datetime.now(UTC)
    assert q["until"].endswith("Z") or q["until"].endswith("+00:00")


def test_quiet_on_without_a_schedule_is_indefinite_and_with_until_ends_there(env):
    c, db = env
    read, ctl = _mint(db, ["read"]), _mint(db, ["control"])
    r = c.post("/api/v1/quiet", json={"mode": "on"}, headers=ctl)
    assert r.status_code == 200
    assert r.json() == {"active": True, "mode": "on", "source": "manual", "until": None}
    assert c.get("/api/v1/quiet", headers=read).json() == r.json()
    until = datetime.now(UTC) + timedelta(hours=2)
    r = c.post("/api/v1/quiet", json={"mode": "on", "until": _iso(until)}, headers=ctl).json()
    assert r["active"] is True and r["source"] == "manual" and r["mode"] == "on"
    assert abs((datetime.fromisoformat(r["until"]) - until).total_seconds()) < 1
    # naive until is read as UTC
    naive = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None, microsecond=0)
    r = c.post("/api/v1/quiet", json={"mode": "on", "until": naive.isoformat()}, headers=ctl).json()
    assert datetime.fromisoformat(r["until"]).replace(tzinfo=None) == naive


def test_quiet_forced_states_end_at_the_next_schedule_boundary_or_until(env):
    """on/off never stick silently: capped at the next scheduled quiet boundary, whichever is first."""
    c, db = env
    read, ctl = _mint(db, ["read"]), _mint(db, ["control"])
    _quiet_schedule(c, db, "00:00", "23:59")                         # scheduled quiet right now
    boundary = datetime.fromisoformat(c.get("/api/v1/quiet", headers=read).json()["until"])
    r = c.post("/api/v1/quiet", json={"mode": "off"}, headers=ctl).json()
    assert r["active"] is False and r["mode"] == "off" and r["source"] == "manual"
    assert datetime.fromisoformat(r["until"]) == boundary
    r = c.post("/api/v1/quiet", json={"mode": "on"}, headers=ctl).json()       # on, no until: capped too
    assert r["active"] is True and datetime.fromisoformat(r["until"]) == boundary
    near = datetime.now(UTC) + timedelta(minutes=5)                            # a nearer until wins
    r = c.post("/api/v1/quiet", json={"mode": "off", "until": _iso(near)}, headers=ctl).json()
    assert abs((datetime.fromisoformat(r["until"]) - near).total_seconds()) < 1
    far = datetime.now(UTC) + timedelta(days=3)                                # a farther one is capped
    r = c.post("/api/v1/quiet", json={"mode": "off", "until": _iso(far)}, headers=ctl).json()
    assert datetime.fromisoformat(r["until"]) == boundary


def test_quiet_auto_clears_the_override(env):
    c, db = env
    ctl = _mint(db, ["control"])
    _quiet_schedule(c, db, "00:00", "23:59")
    c.post("/api/v1/quiet", json={"mode": "off"}, headers=ctl)
    assert c.get("/api/displays/wall/schedule-state").json()["quiet"] is False
    r = c.post("/api/v1/quiet", json={"mode": "auto"}, headers=ctl).json()
    assert r["mode"] == "auto" and r["source"] == "schedule" and r["active"] is True
    assert db.query(SettingsModel).filter_by(setting_key="quiet_override").count() == 0
    assert c.get("/api/displays/wall/schedule-state").json()["quiet"] is True
    # auto when nothing is set is a harmless no-op, and ignores an `until`
    assert c.post("/api/v1/quiet", json={"mode": "auto", "until": "2000-01-01T00:00:00Z"},
                  headers=ctl).status_code == 200


def test_quiet_validation_is_422_and_writes_nothing(env):
    c, db = env
    ctl = _mint(db, ["control"])
    r = c.post("/api/v1/quiet", json={"mode": "on", "until": _iso(datetime.now(UTC) - timedelta(minutes=1))},
               headers=ctl)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert db.query(SettingsModel).filter_by(setting_key="quiet_override").count() == 0
    for body in ({}, {"on": True}, {"mode": "maybe"}, {"mode": True}, {"mode": "on", "x": 1},
                 {"mode": "on", "until": "soon"}):
        assert c.post("/api/v1/quiet", json=body, headers=ctl).status_code == 422, body


def test_quiet_override_expires_and_clears_itself(env):
    c, db = env
    read, ctl = _mint(db, ["read"]), _mint(db, ["control"])
    c.post("/api/v1/quiet", json={"mode": "on", "until": _iso(datetime.now(UTC) + timedelta(hours=1))},
           headers=ctl)
    _expire_override(db, True)
    assert c.get("/api/v1/quiet", headers=read).json() == {"active": False, "mode": "auto",
                                                           "source": "none", "until": None}
    assert db.query(SettingsModel).filter_by(setting_key="quiet_override").count() == 0


def test_quiet_override_reaches_schedule_state_for_canvas_and_cec(env):
    """Both the Canvas blackout and sd-quiet-hours poll the INTERNAL schedule-state route."""
    c, db = env
    read, ctl = _mint(db, ["read"]), _mint(db, ["control"])
    internal = "/api/displays/wall/schedule-state"
    assert c.get(internal).json()["quiet"] is False
    c.post("/api/v1/quiet", json={"mode": "on"}, headers=ctl)
    st = c.get(internal).json()
    assert st["quiet"] is True and st["quiet_mode"] == "cec"          # quiet_enabled is False; override wins
    assert c.get("/api/v1/schedule/state", headers=read).json()["quiet"] is True
    # forced-off beats a scheduled quiet window
    _quiet_schedule(c, db, "00:00", "23:59")
    c.post("/api/v1/quiet", json={"mode": "off"}, headers=ctl)
    assert c.get(internal).json()["quiet"] is False
    assert c.get("/api/v1/schedule/state", headers=read).json()["quiet"] is False
    # an expired override stops mattering
    _expire_override(db, False)
    assert c.get(internal).json()["quiet"] is True                    # back to the schedule


def test_quiet_override_works_with_the_schedule_disabled(env):
    c, db = env
    c.patch("/api/v1/schedule", json={"enabled": False}, headers=_mint(db, ["control"]))
    c.post("/api/v1/quiet", json={"mode": "on"}, headers=_mint(db, ["control"]))
    assert c.get("/api/displays/wall/schedule-state").json()["quiet"] is True


# --- A2 contract additions: playlist_id, updated_at, name, info features, kind rules, id caps -----------

def test_set_playlist_by_id_or_name_exactly_one(env):
    c, db = env
    summer = _playlist(db, "Summer", [_art(db)])
    hidden = _playlist(db, "_internal", [_art(db, "H")])
    _display(db, "wall", kind="canvas")
    h = _mint(db, ["control"])
    r = c.post("/api/v1/displays/wall/commands", json={"action": "set_playlist", "playlist_id": summer.id}, headers=h)
    assert r.status_code == 202
    assert json.loads(db.query(RemoteCommandModel).one().payload) == {"action": "set_playlist", "playlist": "Summer"}
    for body in ({"action": "set_playlist"},
                 {"action": "set_playlist", "playlist": "Summer", "playlist_id": summer.id},
                 {"action": "set_playlist", "playlist": "  "},
                 {"action": "set_playlist", "playlist_id": 0},
                 {"action": "set_playlist", "playlist_id": 10**30}):
        assert c.post("/api/v1/displays/wall/commands", json=body, headers=h).status_code == 422, body
    for body in ({"action": "set_playlist", "playlist_id": 9999},
                 {"action": "set_playlist", "playlist_id": hidden.id}):
        r = c.post("/api/v1/displays/wall/commands", json=body, headers=h)
        assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_playlist"
    assert db.query(RemoteCommandModel).count() == 1


def test_display_name_playlist_id_and_updated_at(env):
    c, db = env
    arts = [_art(db, f"A{i}") for i in range(3)]
    pl = _playlist(db, "Seq", arts)
    _display(db, "wall", age_s=2, kind="canvas")
    read, ctl = _mint(db, ["read"]), _mint(db, ["control"])
    d = c.get("/api/v1/displays/wall", headers=read).json()
    assert d["name"] == "wall" and d["playlist_id"] is None
    assert datetime.fromisoformat(d["updated_at"]) == datetime.fromisoformat(d["last_seen"])   # fallback
    _next(c)
    d1 = c.get("/api/v1/displays/wall", headers=read).json()
    assert d1["playlist"] == "Seq" and d1["playlist_id"] == pl.id
    t1 = datetime.fromisoformat(d1["updated_at"])
    assert t1 > datetime.fromisoformat(d["last_seen"])
    # a held (paused) serve changes nothing; pausing itself does; the next change moves it again
    c.post("/api/v1/displays/wall/commands", json={"action": "pause"}, headers=ctl)
    t2 = datetime.fromisoformat(c.get("/api/v1/displays/wall", headers=read).json()["updated_at"])
    assert t2 >= t1
    _next(c)
    assert datetime.fromisoformat(c.get("/api/v1/displays/wall", headers=read).json()["updated_at"]) == t2
    _next(c, manual="true")
    t3 = datetime.fromisoformat(c.get("/api/v1/displays/wall", headers=read).json()["updated_at"])
    assert t3 > t2
    c.post("/api/v1/displays/wall/commands", json={"action": "set_mode", "mode": "static-crop"}, headers=ctl)
    assert datetime.fromisoformat(c.get("/api/v1/displays/wall", headers=read).json()["updated_at"]) > t3


def test_info_advertises_features_and_the_calling_tokens_scopes(env):
    c, db = env
    i = c.get("/api/v1/info", headers=_mint(db, ["read"])).json()
    assert i["token_scopes"] == ["read"]
    assert {"displays", "commands", "pause", "show", "quiet", "schedule", "search"} <= set(i["api_features"])
    assert sorted(c.get("/api/v1/info", headers=_mint(db, ["read", "control"])).json()["token_scopes"]) \
        == ["control", "read"]


@pytest.mark.parametrize("body", [{"action": "next"}, {"action": "previous"}, {"action": "show_placard"},
                                  {"action": "set_mode", "mode": "static-crop"},
                                  {"action": "set_playlist", "playlist": "Summer"}])
@pytest.mark.parametrize("live", [True, False])
def test_canvas_only_actions_on_pull_based_displays_are_409_unsupported(env, body, live):
    c, db = env
    _playlist(db, "Summer", [_art(db)])
    _display(db, "panel", age_s=1 if live else 3600, kind="eink")
    _display(db, "frame-tv", age_s=1 if live else 3600)
    h = _mint(db, ["control"])
    for did in ("panel", "frame-tv"):
        r = c.post(f"/api/v1/displays/{did}/commands", json=body, headers=h)
        assert r.status_code == 409 and r.json()["error"]["code"] == "unsupported_for_kind", (did, r.text)
    assert db.query(RemoteCommandModel).count() == 0
    # pause/resume and show remain allowed on them
    art = _art(db, "X")
    assert c.post("/api/v1/displays/panel/commands", json={"action": "pause"}, headers=h).status_code == 202
    assert c.post("/api/v1/displays/frame-tv/show", json={"artwork_id": art.id}, headers=h).status_code == 202


def test_unknown_kind_is_treated_as_canvas_and_needs_to_be_live(env):
    c, db = env
    _display(db, "mystery", age_s=1)
    _display(db, "mystery-asleep", age_s=3600)
    h = _mint(db, ["control"])
    assert c.post("/api/v1/displays/mystery/commands", json={"action": "next"}, headers=h).status_code == 202
    r = c.post("/api/v1/displays/mystery-asleep/commands", json={"action": "next"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_live"


@pytest.mark.parametrize("path", ["/api/v1/artworks/99999999999999999999",
                                  "/api/v1/artworks/0", "/api/v1/playlists/99999999999999999999/artworks",
                                  "/api/v1/playlists/1/artworks?offset=99999999999999999999",
                                  "/api/v1/playlists/1/artworks?offset=1000001"])
def test_oversized_ids_and_offsets_are_422_not_500(env, path):
    c, db = env
    r = c.get(path, headers=_mint(db, ["read"]))
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_oversized_show_artwork_id_is_422(env):
    c, db = env
    _display(db, "wall", kind="canvas")
    r = c.post("/api/v1/displays/wall/show", json={"artwork_id": 10**30}, headers=_mint(db, ["control"]))
    assert r.status_code == 422
    assert c.get("/next-image", params={"playlist_name": "x", "artwork_id": 10**30}).status_code == 422


# --- A2: command TTL ---------------------------------------------------------------------------------

def test_expired_commands_are_never_delivered_and_are_purged(env):
    from core.playback import COMMAND_TTL_SEC, take_deliverable_commands
    _, db = env
    old = datetime.now(UTC) - timedelta(seconds=COMMAND_TTL_SEC + 5)
    db.add(RemoteCommandModel(target_display="wall", action="next_image",
                              payload=json.dumps({"action": "next_image"}), created_at=old))
    db.add(RemoteCommandModel(target_display="wall", action="pause", payload=json.dumps({"action": "pause"})))
    db.add(RemoteCommandModel(target_display="other", action="next_image",
                              payload=json.dumps({"action": "next_image"})))
    db.commit()
    assert take_deliverable_commands(db, "wall") == [{"action": "pause"}]
    left = db.query(RemoteCommandModel).all()
    assert [m.target_display for m in left] == ["other"]               # stale + delivered rows are gone
    assert take_deliverable_commands(db, "wall") == []


def test_enqueue_purges_stale_rows_for_any_display_incl_internal_remote_path(env):
    from core.playback import COMMAND_TTL_SEC
    c, db = env
    old = datetime.now(UTC) - timedelta(seconds=COMMAND_TTL_SEC + 5)
    db.add(RemoteCommandModel(target_display="gone", action="next_image",
                              payload=json.dumps({"action": "next_image"}), created_at=old))
    db.commit()
    r = c.post("/api/remote/change", json={"target_display": "wall", "action": "next_image"})
    assert r.status_code == 200
    assert [m.target_display for m in db.query(RemoteCommandModel).all()] == ["wall"]


def test_v1_openapi_describes_the_a2_surface(env):
    c, _ = env
    spec = c.get("/api/v1/openapi.json").json()
    assert "/displays/{display_id}/show" in spec["paths"] and "/quiet" in spec["paths"]
    schemas = spec["components"]["schemas"]
    assert {"pause", "resume"} <= set(schemas["CommandAction"]["enum"])
    assert schemas["Display"]["properties"]["paused"]["type"] == "boolean"
    assert set(schemas["QuietState"]["properties"]) == {"active", "mode", "source", "until"}
    assert set(schemas["QuietRequest"]["properties"]) == {"mode", "until"}
    assert {"playlist", "playlist_id"} <= set(schemas["CommandRequest"]["properties"])
    assert {"name", "playlist_id", "updated_at"} <= set(schemas["Display"]["properties"])
    assert {"api_features", "token_scopes"} <= set(schemas["Info"]["properties"])
    assert set(spec["paths"]["/quiet"]) == {"get", "post"}


# --- review fix: the token-management gate ----------------------------------------------------------

def test_sec_fetch_site_alone_opens_only_the_list_route(env, monkeypatch):
    """Sec-Fetch-Site is forgeable by any non-browser; it is accepted for the browser's Origin-less
    same-origin GET (the list) and NEVER for mint/revoke."""
    c, _ = env
    monkeypatch.setattr(config, "APPLIANCE_UPDATE_TOKEN", "")
    sfs = {"Sec-Fetch-Site": "same-origin"}
    assert c.get("/api/settings/api-tokens", headers=sfs).status_code == 200
    assert c.post("/api/settings/api-tokens", json={"name": "x", "scopes": ["control"]},
                  headers=sfs).status_code == 403                       # curl-style forgery
    assert c.delete("/api/settings/api-tokens/1", headers=sfs).status_code == 403
    assert c.post("/api/settings/api-tokens", json={"name": "x", "scopes": ["control"]},
                  headers={**ADMIN, **sfs}).status_code == 201          # a real same-origin browser POST
