"""Night & Quiet Hours schedule resolver + settings API (R1-F2).

resolve_schedule_state is the pure brain: given the schedule config + a wall-clock time it returns the
brightness/warmth the Canvas overlay should apply and whether the panel should be in quiet hours. The
'night factor' ramps 0->1 across the evening window, holds overnight, and ramps back down in the morning;
quiet hours is an independent opt-in window that must handle wrapping past midnight.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import DEFAULT_SCHEDULE, app, resolve_schedule_state
from database import Base, get_db


def _dt(hh, mm=0):
    return datetime(2026, 1, 1, hh, mm)


# --- pure resolver ------------------------------------------------------------

def test_midday_is_full_day():
    st = resolve_schedule_state(DEFAULT_SCHEDULE, _dt(12))
    assert st["brightness"] == 1.0
    assert st["warmth"] == 0.0
    assert st["quiet"] is False


def test_deep_night_is_full_night():
    st = resolve_schedule_state(DEFAULT_SCHEDULE, _dt(2))
    assert st["brightness"] == DEFAULT_SCHEDULE["night_brightness"]   # 0.72
    assert st["warmth"] == DEFAULT_SCHEDULE["night_warmth"]           # 0.28


def test_evening_ramp_is_interpolated():
    # evening 20:00 -> night 22:30 == 150 min. 21:15 is 75 min in -> n = 0.5.
    st = resolve_schedule_state(DEFAULT_SCHEDULE, _dt(21, 15))
    assert st["brightness"] == pytest.approx(1.0 + (0.72 - 1.0) * 0.5, abs=1e-3)   # 0.86
    assert st["warmth"] == pytest.approx(0.28 * 0.5, abs=1e-3)                      # 0.14


def test_morning_ramp_is_interpolated():
    # morning 06:30 -> day 08:00 == 90 min. 07:15 is 45 min in -> n = 1 - 0.5 = 0.5.
    st = resolve_schedule_state(DEFAULT_SCHEDULE, _dt(7, 15))
    assert st["brightness"] == pytest.approx(0.86, abs=1e-3)
    assert st["warmth"] == pytest.approx(0.14, abs=1e-3)


def test_disabled_is_fully_neutral():
    sched = {**DEFAULT_SCHEDULE, "enabled": False, "quiet_enabled": True}
    st = resolve_schedule_state(sched, _dt(2))     # deep night + quiet-on, but feature off
    assert st == {"enabled": False, "brightness": 1.0, "warmth": 0.0, "quiet": False,
                  "quiet_mode": sched["quiet_mode"]}


@pytest.mark.parametrize("hh,mm,expected", [
    (2, 0, True),     # inside the wrapped window
    (23, 45, True),   # just after quiet_start
    (6, 59, True),    # just before quiet_end
    (7, 30, False),   # after quiet_end
    (12, 0, False),   # midday
])
def test_quiet_window_wraps_midnight(hh, mm, expected):
    sched = {**DEFAULT_SCHEDULE, "quiet_enabled": True, "quiet_start": "23:30", "quiet_end": "07:00"}
    assert resolve_schedule_state(sched, _dt(hh, mm))["quiet"] is expected


def test_quiet_off_by_default():
    # Default schedule has quiet_enabled False, so even at 3am the panel is never put to sleep.
    assert resolve_schedule_state(DEFAULT_SCHEDULE, _dt(3))["quiet"] is False


# --- settings + state API -----------------------------------------------------

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


def test_get_schedule_returns_defaults(client):
    r = client.get("/api/settings/display-schedule")
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    assert r.json()["quiet_enabled"] is False


def test_post_schedule_merges_and_persists(client):
    r = client.post("/api/settings/display-schedule", json={"night_brightness": 0.5, "quiet_enabled": True})
    assert r.status_code == 200
    assert r.json()["night_brightness"] == 0.5
    assert r.json()["quiet_enabled"] is True
    # Untouched fields keep their defaults (merge, not replace).
    assert r.json()["night_warmth"] == DEFAULT_SCHEDULE["night_warmth"]
    # And it round-trips.
    assert client.get("/api/settings/display-schedule").json()["night_brightness"] == 0.5


@pytest.mark.parametrize("bad", [
    {"night_brightness": 1.5},     # out of range
    {"day_brightness": 0.0},       # below floor
    {"night_warmth": 2.0},         # out of range
    {"evening_start": "8pm"},      # not HH:MM
    {"quiet_mode": "laser"},       # not an allowed mode
])
def test_post_schedule_rejects_bad_values(client, bad):
    assert client.post("/api/settings/display-schedule", json=bad).status_code == 400


def test_schedule_state_endpoint_with_now_override(client):
    night = client.get("/api/displays/wall/schedule-state", params={"now": "02:00"}).json()
    assert night["brightness"] == DEFAULT_SCHEDULE["night_brightness"]
    assert night["warmth"] == DEFAULT_SCHEDULE["night_warmth"]

    day = client.get("/api/displays/wall/schedule-state", params={"now": "12:00"}).json()
    assert day["brightness"] == 1.0 and day["warmth"] == 0.0


def test_schedule_state_rejects_bad_now(client):
    assert client.get("/api/displays/wall/schedule-state", params={"now": "25:99x"}).status_code == 400
