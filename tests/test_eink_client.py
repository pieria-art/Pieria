"""
Unit tests for the e-ink device client (Track B).

Covers everything except actual panel acceptance (no hardware — same posture as
no-frame-tv-for-testing): pull/dedupe (etag + content-hash fallback), cadence-floor sleep math,
quiet-hours suppression, error handling, orientation rotation, and that InkyClient drives the
`inky` API correctly via an injected fake factory (mirroring test_samsung_client_maps_to_library_api).
Also asserts the `/display/{id}/current.png` route carries the ETag header eink_client relies on.
"""

import hashlib
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import eink_client as ec


def _cfg(**over):
    base = dict(server_url="http://localhost:8000", display_id="wall", min_interval=900,
                saturation=0.5, orientation="")
    base.update(over)
    return ec.EinkConfig(**base)


def _png_bytes(size=(1600, 1200), color=(120, 40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _fetch_fn_for(content: bytes, headers: dict):
    def _fetch(_url):
        return content, headers
    return _fetch


# ------------------------------------------------------------------ push_once

def test_push_once_first_call_paints():
    content = _png_bytes()
    client = ec.FakeInkyClient()
    fetch_fn = _fetch_fn_for(content, {"ETag": '"abc123"'})

    res = ec.push_once(_cfg(), fetch_fn, client, last_etag=None)

    assert res["status"] == "painted"
    assert res["etag"] == '"abc123"'
    assert len(client.calls) == 1
    assert client.shown == [((1600, 1200), 0.5)]


def test_push_once_same_etag_is_unchanged():
    content = _png_bytes()
    client = ec.FakeInkyClient()
    fetch_fn = _fetch_fn_for(content, {"ETag": '"abc123"'})

    res = ec.push_once(_cfg(), fetch_fn, client, last_etag='"abc123"')

    assert res["status"] == "unchanged"
    assert res["etag"] == '"abc123"'
    assert client.calls == []


def test_push_once_changed_etag_repaints():
    content = _png_bytes()
    client = ec.FakeInkyClient()
    fetch_fn = _fetch_fn_for(content, {"ETag": '"new-etag"'})

    res = ec.push_once(_cfg(), fetch_fn, client, last_etag='"old-etag"')

    assert res["status"] == "painted"
    assert res["etag"] == '"new-etag"'
    assert len(client.calls) == 1


def test_push_once_content_hash_fallback_when_no_etag_header():
    content = _png_bytes()
    client = ec.FakeInkyClient()
    fetch_fn = _fetch_fn_for(content, {})  # no ETag header at all

    res = ec.push_once(_cfg(), fetch_fn, client, last_etag=None)

    expected = "sha256:" + hashlib.sha256(content).hexdigest()[:16]
    assert res["status"] == "painted"
    assert res["etag"] == expected

    # Same body again, still no header -> same computed hash -> dedupe still works.
    res2 = ec.push_once(_cfg(), fetch_fn, client, last_etag=res["etag"])
    assert res2["status"] == "unchanged"
    assert len(client.calls) == 1  # second call did NOT repaint


def test_push_once_refresh_after_header_is_read():
    content = _png_bytes()
    client = ec.FakeInkyClient()
    fetch_fn = _fetch_fn_for(content, {"ETag": '"e1"', "X-Refresh-After": "45"})

    res = ec.push_once(_cfg(), fetch_fn, client, last_etag=None)
    assert res["refresh_after"] == 45


def test_portrait_requests_a_portrait_composition():
    """A portrait-hung panel must ASK the server for a portrait frame.

    Regression: pull_url used to hardcode w=1600&h=1200, so the server composed for a LANDSCAPE
    window and the client just rotated the result — a portrait frame never received a portrait
    composition, and the per-aspect crop presets would have been picked for the wrong shape.
    """
    assert "w=1600&h=1200" in _cfg(orientation="").pull_url
    assert "w=1200&h=1600" in _cfg(orientation="portrait").pull_url


def test_push_once_portrait_paints_the_panels_native_buffer():
    """After rotation the panel must get its NATIVE landscape buffer, not a transposed one.

    Regression: portrait mode used to hand a 1200x1600 image to a 1600x1200 panel.
    """
    landscape_client = ec.FakeInkyClient()
    portrait_client = ec.FakeInkyClient()

    ec.push_once(_cfg(orientation=""), _fetch_fn_for(_png_bytes(size=(1600, 1200)),
                 {"ETag": '"e1"'}), landscape_client, last_etag=None)
    # the server now answers a portrait request with a portrait-composed frame
    ec.push_once(_cfg(orientation="portrait"), _fetch_fn_for(_png_bytes(size=(1200, 1600)),
                 {"ETag": '"e1"'}), portrait_client, last_etag=None)

    assert landscape_client.shown[0][0] == (1600, 1200)
    assert portrait_client.shown[0][0] == (1600, 1200)  # rotated back onto the native buffer


# ------------------------------------------------------------------ run_tick

def test_run_tick_cadence_floor_wins_over_short_refresh_after():
    content = _png_bytes()
    client = ec.FakeInkyClient()
    fetch_fn = _fetch_fn_for(content, {"ETag": '"e1"', "X-Refresh-After": "30"})
    cfg = _cfg(min_interval=900)

    res = ec.run_tick(cfg, fetch_fn, client, lambda: {"quiet": False}, {})

    assert res["status"] == "painted"
    assert res["sleep"] == 900  # floor wins over the 30s refresh_after


def test_run_tick_refresh_after_wins_when_above_floor():
    content = _png_bytes()
    client = ec.FakeInkyClient()
    fetch_fn = _fetch_fn_for(content, {"ETag": '"e1"', "X-Refresh-After": "1800"})
    cfg = _cfg(min_interval=900)

    res = ec.run_tick(cfg, fetch_fn, client, lambda: {"quiet": False}, {})

    assert res["status"] == "painted"
    assert res["sleep"] == 1800  # above the floor -> refresh_after wins


def test_run_tick_quiet_hours_skips_paint():
    content = _png_bytes()
    client = ec.FakeInkyClient()
    fetch_fn = _fetch_fn_for(content, {"ETag": '"e1"'})

    res = ec.run_tick(_cfg(), fetch_fn, client, lambda: {"quiet": True}, {})

    assert res["status"] == "quiet"
    assert client.calls == []


def test_run_tick_fetch_error_is_caught():
    def _boom(_url):
        raise ConnectionError("no route to host")

    client = ec.FakeInkyClient()
    res = ec.run_tick(_cfg(min_interval=900), _boom, client, lambda: {"quiet": False}, {})

    assert res["status"] == "error"
    # First failure retries FAST (not the 900s steady-state interval): a device that outruns its own
    # server at boot must recover in seconds, not sit on a stale image for 15 minutes.
    assert res["sleep"] == ec.ERROR_BACKOFF_BASE
    assert res["attempt"] == 1
    assert client.calls == []


def test_run_tick_show_error_is_caught_and_state_not_updated():
    content = _png_bytes()
    client = ec.FakeInkyClient(fail_on="show")
    fetch_fn = _fetch_fn_for(content, {"ETag": '"e1"'})
    state = {}

    res = ec.run_tick(_cfg(min_interval=900), fetch_fn, client, lambda: {"quiet": False}, state)

    assert res["status"] == "error"
    assert res["sleep"] == ec.ERROR_BACKOFF_BASE
    assert "last_etag" not in state


def test_run_tick_schedule_fn_error_does_not_block_painting():
    content = _png_bytes()
    client = ec.FakeInkyClient()
    fetch_fn = _fetch_fn_for(content, {"ETag": '"e1"'})

    def _boom_schedule():
        raise RuntimeError("schedule endpoint down")

    res = ec.run_tick(_cfg(), fetch_fn, client, _boom_schedule, {})

    assert res["status"] == "painted"
    assert len(client.calls) == 1


def test_run_tick_updates_state_on_paint():
    content = _png_bytes()
    client = ec.FakeInkyClient()
    fetch_fn = _fetch_fn_for(content, {"ETag": '"e1"'})
    state = {}

    ec.run_tick(_cfg(), fetch_fn, client, lambda: {"quiet": False}, state)

    assert state["last_etag"] == '"e1"'


# ------------------------------------------------------------------ InkyClient hardware mapping

class _FakeInky:
    def __init__(self, rec):
        self.rec = rec
        self.resolution = (1600, 1200)

    def set_image(self, image, saturation=None):
        self.rec.append(("set_image", image.size, saturation))

    def show(self):
        self.rec.append(("show",))


def test_inky_client_maps_to_library_api():
    rec = []
    client = ec.InkyClient(inky_factory=lambda: _FakeInky(rec))

    img = Image.new("RGB", (1600, 1200), (10, 20, 30))
    client.show(img, 0.7)

    assert ("set_image", (1600, 1200), 0.7) in rec
    assert ("show",) in rec
    assert client.resolution == (1600, 1200)


# ------------------------------------------------------------------ /display/{id}/current.png ETag

@pytest.fixture
def display_client(tmp_path, monkeypatch):
    import app as app_module
    import routers.display as routers_display
    from app import app
    from database import Base, get_db

    monkeypatch.setattr(app_module, "LIBRARY_DIR", tmp_path / "_Library")
    monkeypatch.setattr(routers_display, "LIBRARY_DIR", tmp_path / "_Library")

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


def test_display_current_route_carries_etag_header(display_client):
    from models import ArtworkModel, PlaylistModel, playlist_artwork

    c, db = display_client
    import app as app_module
    app_module.LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 300), (200, 60, 10)).save(app_module.LIBRARY_DIR / "e.jpg", format="JPEG")

    pl = PlaylistModel(name="Eink", shuffle=False)
    db.add(pl); db.commit(); db.refresh(pl)
    art = ArtworkModel(filename="e.jpg", status="approved", title="Test")
    db.add(art); db.commit(); db.refresh(art)
    db.execute(playlist_artwork.insert().values(playlist_id=pl.id, artwork_id=art.id, display_order=0))
    db.commit()

    r = c.get("/display/bench/current.png", params={"playlist": "Eink", "w": 200, "h": 150})
    assert r.status_code == 200
    assert "etag" in r.headers
    assert r.headers["etag"]


def test_error_backoff_escalates_then_resets_on_success():
    """Consecutive failures back off 15s -> 30s -> 60s ... capped at min_interval, and a single success
    clears the streak. The cap matters as much as the escalation: without it a long outage would retry
    forever at 15s intervals; without the reset, one boot-time blip would keep the panel in fast-retry
    for the rest of its life. Regression guard for the 2026-07-21 finding where any error slept 900s."""
    calls = {"n": 0}

    def _flaky(_url):
        calls["n"] += 1
        raise ConnectionError("connection refused")

    cfg = _cfg(min_interval=900)
    client = ec.FakeInkyClient()
    state: dict = {}

    sleeps = []
    for _ in range(8):
        res = ec.run_tick(cfg, _flaky, client, lambda: {"quiet": False}, state)
        sleeps.append(res["sleep"])

    assert sleeps == [15, 30, 60, 120, 240, 480, 900, 900]  # doubles, then pinned to min_interval

    # A success resets the streak, so the NEXT failure starts short again.
    ok_fetch = _fetch_fn_for(_png_bytes(), {"ETag": '"ok"'})
    ec.run_tick(cfg, ok_fetch, ec.FakeInkyClient(), lambda: {"quiet": False}, state)
    assert state["error_streak"] == 0

    res = ec.run_tick(cfg, _flaky, client, lambda: {"quiet": False}, state)
    assert res["sleep"] == ec.ERROR_BACKOFF_BASE
