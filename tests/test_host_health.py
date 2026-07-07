"""Device Health — throttle-bitmask decode, graceful degradation, and endpoint mode-gating."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
import host_health
from app import app
from database import Base, get_db

# --- decode_throttled (pure) ---------------------------------------------------------------------

def test_decode_throttled_clean():
    out = host_health.decode_throttled(0x0)
    assert out == {"raw": "0x0", "active": [], "occurred": []}


def test_decode_throttled_bits():
    # 0x50005: low nibble 0x5 = bits 0+2 active; high nibble 0x5_0000 = bits 16+18 occurred.
    out = host_health.decode_throttled(0x50005)
    assert out["raw"] == "0x50005"
    assert set(out["active"]) == {"under-voltage", "currently-throttled"}
    assert set(out["occurred"]) == {"under-voltage", "currently-throttled"}


# --- graceful degradation (dev/CI: no vcgencmd, often no thermal zone) ---------------------------

def test_collect_never_raises_and_has_all_keys():
    snap = host_health.collect()
    for key in ("loadavg", "temp_c", "memory", "uptime_s", "disk", "throttled", "watchdog"):
        assert key in snap


def test_read_throttled_unavailable_without_file_or_vcgencmd(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path)  # empty dir → no host_metrics.json
    monkeypatch.setattr(host_health, "_read_vcgencmd_throttled", lambda: None)
    assert host_health.read_throttled() == "unavailable"


def test_read_watchdog_none_without_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path)  # empty dir → no watchdog.json
    assert host_health.read_watchdog() is None


def test_read_watchdog_returns_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path)
    (tmp_path / "watchdog.json").write_text(
        '{"mode":"observe","server_ok":1,"kiosk_ok":0,"action":"observe:relaunch-kiosk"}')
    wd = host_health.read_watchdog()
    assert wd["mode"] == "observe"
    assert wd["action"] == "observe:relaunch-kiosk"


def test_read_throttled_from_host_writer_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path)
    (tmp_path / "host_metrics.json").write_text('{"throttled": "0x50000"}')
    out = host_health.read_throttled()
    assert out["raw"] == "0x50000"
    assert set(out["occurred"]) == {"under-voltage", "currently-throttled"}
    assert out["active"] == []


# --- endpoint mode-gating ------------------------------------------------------------------------

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


def test_host_health_404_when_not_appliance(client, monkeypatch):
    monkeypatch.setattr(config, "IS_APPLIANCE", False)
    assert client.get("/api/health/host").status_code == 404


def test_host_health_200_when_appliance(client, monkeypatch):
    monkeypatch.setattr(config, "IS_APPLIANCE", True)
    resp = client.get("/api/health/host")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert set(body["host"].keys()) == {"loadavg", "temp_c", "memory", "uptime_s", "disk", "throttled", "watchdog"}
    assert isinstance(body["displays"], list)
