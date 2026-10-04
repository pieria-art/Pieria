"""Sunrise/sunset auto-dimming (ADR-148): NOAA calc, zone -> coords, polar fallback, offsets, validation."""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import DEFAULT_SCHEDULE, app, resolve_schedule_state
from core import settings_util, sun
from database import Base, get_db


def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# (date, lat, lon, utc offset min, sunrise, sunset) — timeanddate.com values
KNOWN = [
    (date(2026, 6, 21), 51.5074, -0.1278, 60, "04:43", "21:22"),      # London, BST
    (date(2026, 12, 21), -33.8688, 151.2093, 660, "05:41", "20:05"),  # Sydney, AEDT
    (date(2026, 12, 21), 64.1466, -21.9426, 0, "11:22", "15:30"),     # Reykjavik, midwinter
]


@pytest.mark.parametrize("day,lat,lon,off,rise,sset", KNOWN)
def test_noaa_matches_known_values(day, lat, lon, off, rise, sset):
    r, s = sun.sun_times_local(day, lat, lon, off)
    assert abs(r - _mins(rise)) <= 2 and abs(s - _mins(sset)) <= 2


def test_polar_day_and_night_have_no_sun_times():
    assert sun.sun_times_local(date(2026, 6, 21), 78.2, 15.6, 120) is None    # Svalbard midnight sun
    assert sun.sun_times_local(date(2026, 12, 21), 78.2, 15.6, 60) is None    # polar night


def test_iso6709_and_zone_lookup():
    assert sun.parse_iso6709("+513030-0000731") == pytest.approx((51.5083, -0.1253), abs=1e-3)
    assert sun.parse_iso6709("+4852+00220") == pytest.approx((48.8667, 2.3333), abs=1e-3)
    assert sun.parse_iso6709("garbage") is None
    assert sun.zone_coords("Europe/London") == pytest.approx((51.508, -0.125), abs=0.01)
    assert sun.zone_coords("America/Chicago")[0] == pytest.approx(41.85, abs=0.1)
    assert sun.zone_coords("Nope/Nowhere") is None and sun.zone_coords(None) is None


def test_location_manual_beats_zone_and_zone_is_default(monkeypatch):
    monkeypatch.setattr(sun, "current_zone", lambda: "Europe/London")
    assert sun.resolve_location({"latitude": 10.0, "longitude": 20.0}) == (10.0, 20.0, "manual")
    lat, lon, src = sun.resolve_location({})
    assert src == "zone:Europe/London" and lat == pytest.approx(51.508, abs=0.01)
    monkeypatch.setattr(sun, "current_zone", lambda: None)
    assert sun.resolve_location({}) is None


# --- resolver wiring ------------------------------------------------------------------------------------

LONDON = {**DEFAULT_SCHEDULE, "mode": "sun", "latitude": 51.5074, "longitude": -0.1278}


@pytest.fixture(autouse=True)
def _bst(monkeypatch):
    monkeypatch.setattr(sun, "_local_offset_min", lambda day: 60.0)
    sun._cached_sun.cache_clear()
    settings_util._warned_no_location = False


def _dt(h, m=0):
    return datetime(2026, 6, 21, h, m)


def test_fixed_mode_is_unchanged():
    fixed = {**DEFAULT_SCHEDULE, "mode": "fixed", "latitude": 51.5, "longitude": -0.1}
    for h in (2, 7, 12, 21, 23):
        assert resolve_schedule_state(fixed, _dt(h)) == resolve_schedule_state(DEFAULT_SCHEDULE, _dt(h))


def test_sun_mode_applies_offsets():
    # London 21 Jun: sunset ~21:22 -> evening ramp starts ~20:52, full night ~22:22; sunrise ~04:43.
    assert resolve_schedule_state(LONDON, _dt(18))["brightness"] == 1.0           # well before the ramp
    mid = resolve_schedule_state(LONDON, _dt(21, 37))                              # halfway up the evening ramp
    assert DEFAULT_SCHEDULE["night_brightness"] < mid["brightness"] < 1.0
    assert resolve_schedule_state(LONDON, _dt(23))["brightness"] == DEFAULT_SCHEDULE["night_brightness"]
    assert resolve_schedule_state(LONDON, _dt(3))["warmth"] == DEFAULT_SCHEDULE["night_warmth"]
    assert resolve_schedule_state(LONDON, _dt(12))["warmth"] == 0.0
    # The fixed 20:00 ramp would already be dimming at 20:30; sun mode (ramp starts ~20:52) is not.
    assert resolve_schedule_state(DEFAULT_SCHEDULE, _dt(20, 30))["brightness"] < 1.0
    assert resolve_schedule_state(LONDON, _dt(20, 30))["brightness"] == 1.0
    # Offsets shift the points: +120 min of evening offset delays dimming past 22:00.
    late = {**LONDON, "evening_offset_min": 120, "night_offset_min": 180}
    assert resolve_schedule_state(late, _dt(22))["brightness"] == 1.0


def test_polar_day_falls_back_to_fixed_times():
    svalbard = {**DEFAULT_SCHEDULE, "mode": "sun", "latitude": 78.2, "longitude": 15.6}
    for h in (2, 21, 23):
        assert resolve_schedule_state(svalbard, _dt(h)) == resolve_schedule_state(DEFAULT_SCHEDULE, _dt(h))


def test_no_location_falls_back_to_fixed_with_warning(monkeypatch, caplog):
    monkeypatch.setattr(sun, "current_zone", lambda: None)
    s = {**DEFAULT_SCHEDULE, "mode": "sun"}
    with caplog.at_level("WARNING", logger="artwork-display-api"):
        assert resolve_schedule_state(s, _dt(21)) == resolve_schedule_state(DEFAULT_SCHEDULE, _dt(21))
    assert "falling back to the fixed" in caplog.text


def test_zone_default_location_drives_sun_mode(monkeypatch):
    monkeypatch.setattr(sun, "current_zone", lambda: "Europe/London")
    s = {**DEFAULT_SCHEDULE, "mode": "sun"}
    assert resolve_schedule_state(s, _dt(18))["brightness"] == 1.0
    assert resolve_schedule_state(s, _dt(23))["brightness"] == DEFAULT_SCHEDULE["night_brightness"]


# --- validation + API round trip --------------------------------------------------------------------------

@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    db.close()


@pytest.mark.parametrize("bad", [
    {"mode": "moon"}, {"evening_offset_min": 181}, {"day_offset_min": -181},
    {"latitude": 91, "longitude": 0}, {"latitude": 0, "longitude": -181}, {"latitude": 10.0},
])
def test_validation_bounds(client, bad):
    assert client.post("/api/settings/display-schedule", json=bad).status_code == 400


def test_defaults_and_settings_round_trip_and_clear_location(client):
    s = client.get("/api/settings/display-schedule").json()
    assert s["mode"] == "fixed" and s["evening_offset_min"] == -30 and s["night_offset_min"] == 60
    assert s["morning_offset_min"] == -30 and s["day_offset_min"] == 30 and s["latitude"] is None
    r = client.post("/api/settings/display-schedule", json={
        "mode": "sun", "evening_offset_min": -180, "day_offset_min": 180, "latitude": -90, "longitude": 180})
    assert r.status_code == 200 and r.json()["mode"] == "sun" and r.json()["latitude"] == -90
    r = client.post("/api/settings/display-schedule", json={"latitude": None, "longitude": None})
    assert r.json()["latitude"] is None and r.json()["longitude"] is None and r.json()["mode"] == "sun"


def test_sun_hint_endpoint(client, monkeypatch):
    monkeypatch.setattr(sun, "current_zone", lambda: "Europe/London")
    j = client.get("/api/settings/display-schedule/sun").json()
    assert j["zone"] == "Europe/London" and j["location"]["source"] == "zone:Europe/London"
    assert ":" in j["sunrise"] and ":" in j["sunset"]
