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
    for key in ("loadavg", "temp_c", "memory", "uptime_s", "disk", "throttled", "watchdog",
                "conf", "os_updates", "support_bundle", "last_update_log", "container_tz"):
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
    assert set(body["host"].keys()) == {"loadavg", "temp_c", "memory", "uptime_s", "disk", "throttled",
                                        "watchdog", "conf", "os_updates", "support_bundle",
                                        "last_update_log", "container_tz"}
    assert isinstance(body["displays"], list)


# --- ADR-119 readers: the root-written mailbox files the Devices UI reads -------------------------

def test_new_readers_are_none_without_their_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path)
    assert host_health.read_conf() is None
    assert host_health.read_os_updates() is None
    assert host_health.read_last_update_log() is None
    assert host_health.read_support_bundle() == {"exists": False}


def test_new_readers_survive_a_half_written_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path)
    (tmp_path / "conf.json").write_text('{"values": {"DISPL')
    assert host_health.read_conf() is None


def test_read_conf_returns_the_exported_mirror(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path)
    (tmp_path / "conf.json").write_text(
        '{"values": {"DISPLAY_ID": "living_room", "TIMEZONE": "America/Chicago"},'
        ' "host_timezone": "America/Chicago"}')
    conf = host_health.read_conf()
    assert conf["values"]["DISPLAY_ID"] == "living_room"
    assert conf["host_timezone"] == "America/Chicago"


def test_read_os_updates_returns_the_check_result(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path)
    (tmp_path / "os-updates.json").write_text('{"count": 69, "reboot_likely": true}')
    assert host_health.read_os_updates() == {"count": 69, "reboot_likely": True}


def test_read_support_bundle_reports_size_and_age(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path)
    (tmp_path / "support-bundle.tar.gz").write_bytes(b"x" * 1234)
    info = host_health.read_support_bundle()
    assert info["exists"] is True
    assert info["size_bytes"] == 1234
    assert info["created_at"].endswith("Z")


def test_read_last_update_log_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path)
    (tmp_path / "last-update.log").write_text("\n".join(f"line {i}" for i in range(200)))
    lines = host_health.read_last_update_log()
    assert len(lines) == 60
    assert lines[-1] == "line 199"


def test_container_tz_reports_this_process_clock():
    tz = host_health.container_tz()
    assert isinstance(tz["tzname"], list) and tz["tzname"]
    assert isinstance(tz["offset_s"], int)


# --- a renamed display carries its server-side state across (ADR-119) -----------------------------

def test_set_display_name_migrates_playback_and_commands_and_clears_the_old_row(
        client, monkeypatch, tmp_path):
    import json as _json

    from models import ActiveDisplayModel, DisplayPlaybackSessionModel, PlaylistModel, RemoteCommandModel
    monkeypatch.setattr(config, "IS_APPLIANCE", True)
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "conf.json").write_text(_json.dumps({"values": {"DISPLAY_ID": "old_name"}}))

    db = next(next(iter(app.dependency_overrides.values()))())
    playlist = PlaylistModel(name="Summer")
    db.add(playlist)
    db.commit()
    db.add_all([
        DisplayPlaybackSessionModel(display_id="old_name", playlist_id=playlist.id),
        RemoteCommandModel(target_display="old_name", action="next_image"),
        ActiveDisplayModel(display_id="old_name"),
    ])
    db.commit()

    resp = client.post("/api/appliance/update", headers={"Origin": "http://testserver"},
                       json={"action": "set-display-name", "display_id": "New Name!"})
    assert resp.status_code == 200

    db.expire_all()
    assert db.query(DisplayPlaybackSessionModel).one().display_id == "new_name"
    assert db.query(RemoteCommandModel).one().target_display == "new_name"
    assert db.query(ActiveDisplayModel).count() == 0
