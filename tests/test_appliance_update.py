"""Appliance update bridge — mode-gating, action whitelist, and request/status file protocol."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import config
from app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Point the bridge at a throwaway dir so tests never touch the real ./data.
    monkeypatch.setattr(config, "APPLIANCE_DIR", tmp_path / "appliance")
    # The admin GUI is same-origin; send what a browser sends so the N6 fail-closed gate lets it through.
    with TestClient(app, headers={"Origin": "http://testserver"}) as c:
        yield c


def test_update_403_when_not_appliance(client, monkeypatch):
    monkeypatch.setattr(config, "IS_APPLIANCE", False)
    assert client.post("/api/appliance/update", json={"action": "update-app"}).status_code == 403
    assert client.get("/api/appliance/update/status").status_code == 403


def test_update_rejects_unknown_action(client, monkeypatch):
    monkeypatch.setattr(config, "IS_APPLIANCE", True)
    resp = client.post("/api/appliance/update", json={"action": "rm -rf /"})
    assert resp.status_code == 400


def test_update_queues_request_and_status(client, monkeypatch):
    monkeypatch.setattr(config, "IS_APPLIANCE", True)
    resp = client.post("/api/appliance/update", json={"action": "update-app"})
    assert resp.status_code == 200
    nonce = resp.json()["nonce"]
    assert nonce

    # request.json carries the action + matching nonce for the host helper.
    req = json.loads((config.APPLIANCE_DIR / "request.json").read_text())
    assert req["action"] == "update-app"
    assert req["nonce"] == nonce
    assert "requested_at" in req

    # status.json was written queued BEFORE the request (so the .path trigger always sees a status).
    status = client.get("/api/appliance/update/status").json()
    assert status["state"] == "queued"
    assert status["action"] == "update-app"
    assert status["nonce"] == nonce


def test_status_idle_when_no_request(client, monkeypatch):
    monkeypatch.setattr(config, "IS_APPLIANCE", True)
    assert client.get("/api/appliance/update/status").json() == {"state": "idle"}


# --- ADR-119: the settings actions, their validation, and the busy guard -------------------------

@pytest.fixture
def appliance(client, monkeypatch):
    monkeypatch.setattr(config, "IS_APPLIANCE", True)
    return client


def _req(client):
    return json.loads((config.APPLIANCE_DIR / "request.json").read_text())


def test_new_actions_are_whitelisted():
    from routers.health import ALLOWED_UPDATE_ACTIONS
    assert len(ALLOWED_UPDATE_ACTIONS) == 16
    for action in ("set-timezone", "set-orientation", "preview-orientation", "set-display-name",
                   "set-watchdog", "set-os-schedule", "reopen-setup", "relaunch-kiosk",
                   "restart-app", "poweroff", "support-bundle", "check-os-updates", "update-system"):
        assert action in ALLOWED_UPDATE_ACTIONS


@pytest.mark.parametrize("action,field", [
    ("set-timezone", "timezone"), ("set-orientation", "orientation"),
    ("set-display-name", "display_id"), ("set-watchdog", "watchdog"),
    ("set-os-schedule", "schedule"),
])
def test_a_missing_required_field_is_a_400(appliance, action, field):
    resp = appliance.post("/api/appliance/update", json={"action": action})
    assert resp.status_code == 400
    assert field in resp.json()["detail"]


@pytest.mark.parametrize("action,body", [
    ("set-timezone", {"timezone": "Not/AZone"}),
    ("set-timezone", {"timezone": "America/Chicago; rm -rf /"}),
    ("set-orientation", {"orientation": "45"}),
    ("set-watchdog", {"watchdog": "on"}),
    ("set-os-schedule", {"schedule": "daily"}),
    ("set-os-schedule", {"schedule": "weekly", "time": "25:00"}),
])
def test_an_invalid_value_is_a_400_and_queues_nothing(appliance, action, body):
    resp = appliance.post("/api/appliance/update", json={"action": action, **body})
    assert resp.status_code == 400
    assert not (config.APPLIANCE_DIR / "request.json").exists()


def test_a_valid_timezone_is_queued(appliance):
    assert appliance.post("/api/appliance/update",
                          json={"action": "set-timezone", "timezone": "America/Chicago"}).status_code == 200
    assert _req(appliance)["timezone"] == "America/Chicago"


def test_the_display_name_is_sanitized_before_it_is_queued(appliance):
    assert appliance.post("/api/appliance/update",
                          json={"action": "set-display-name", "display_id": "Living Room!"}).status_code == 200
    assert _req(appliance)["display_id"] == "living_room"


def test_a_display_name_that_sanitizes_to_nothing_is_a_400(appliance):
    assert appliance.post("/api/appliance/update",
                          json={"action": "set-display-name", "display_id": "!!!"}).status_code == 400


def test_the_orientation_choice_is_transmitted_not_the_rotate_value(appliance):
    # The host maps landscape -> ROTATE= (blank) and derives EINK_ORIENTATION; sending "" here would
    # be indistinguishable from a missing field.
    assert appliance.post("/api/appliance/update",
                          json={"action": "set-orientation", "orientation": "landscape"}).status_code == 200
    assert _req(appliance)["orientation"] == "landscape"


def test_undeclared_fields_never_reach_the_request_file(appliance):
    resp = appliance.post("/api/appliance/update", json={
        "action": "set-watchdog", "watchdog": "enforce",
        "timezone": "America/Chicago", "display_id": "elsewhere", "orientation": "90"})
    assert resp.status_code == 200
    req = _req(appliance)
    assert req["watchdog"] == "enforce"
    assert set(req) == {"action", "requested_at", "nonce", "watchdog"}


def test_an_optional_field_may_be_omitted(appliance):
    assert appliance.post("/api/appliance/update",
                          json={"action": "set-os-schedule", "schedule": "off"}).status_code == 200
    assert "time" not in _req(appliance)


def test_a_fieldless_action_carries_no_extra_keys(appliance):
    assert appliance.post("/api/appliance/update", json={"action": "poweroff"}).status_code == 200
    assert set(_req(appliance)) == {"action", "requested_at", "nonce"}


# --- 409 busy guard --------------------------------------------------------------------------------

def _status(state, **extra):
    (config.APPLIANCE_DIR).mkdir(parents=True, exist_ok=True)
    (config.APPLIANCE_DIR / "status.json").write_text(json.dumps(
        {"state": state, "action": "update-system", **extra}))


def test_a_running_action_blocks_a_second_one(appliance):
    _status("running", updated_at=datetime.now(UTC).isoformat())
    resp = appliance.post("/api/appliance/update", json={"action": "reboot"})
    assert resp.status_code == 409
    assert "update-system" in resp.json()["detail"]


def test_the_queued_status_carries_queued_at_and_blocks_too(appliance):
    assert appliance.post("/api/appliance/update", json={"action": "reboot"}).status_code == 200
    assert appliance.get("/api/appliance/update/status").json()["queued_at"]
    assert appliance.post("/api/appliance/update", json={"action": "reboot"}).status_code == 409


def test_a_stale_running_status_does_not_block(appliance):
    # A reboot mid-action strands `running` forever; the only management surface this box has must
    # not be permanently locked by it.
    _status("running", updated_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat())
    assert appliance.post("/api/appliance/update", json={"action": "reboot"}).status_code == 200


def test_a_corrupt_status_does_not_block(appliance):
    (config.APPLIANCE_DIR).mkdir(parents=True, exist_ok=True)
    (config.APPLIANCE_DIR / "status.json").write_text("{not json")
    assert appliance.post("/api/appliance/update", json={"action": "reboot"}).status_code == 200


def test_a_finished_action_does_not_block(appliance):
    _status("done", updated_at=datetime.now(UTC).isoformat())
    assert appliance.post("/api/appliance/update", json={"action": "reboot"}).status_code == 200


# --- support bundle download -------------------------------------------------------------------

def test_support_bundle_404_before_one_exists(appliance):
    assert appliance.get("/api/appliance/support-bundle").status_code == 404


def test_support_bundle_downloads(appliance):
    config.APPLIANCE_DIR.mkdir(parents=True, exist_ok=True)
    (config.APPLIANCE_DIR / "support-bundle.tar.gz").write_bytes(b"\x1f\x8b\x08 not really gzip")
    resp = appliance.get("/api/appliance/support-bundle")
    assert resp.status_code == 200
    assert resp.content == b"\x1f\x8b\x08 not really gzip"


def test_support_bundle_403_when_not_an_appliance(client, monkeypatch):
    monkeypatch.setattr(config, "IS_APPLIANCE", False)
    assert client.get("/api/appliance/support-bundle").status_code == 403


# ── N6: the bridge fails CLOSED for non-browser callers ──────────────────────────────────────────────

def _bare(**headers):
    return TestClient(app, headers=headers)


def test_no_origin_no_token_is_refused(appliance, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_UPDATE_TOKEN", "")
    with _bare() as c:
        assert c.post("/api/appliance/update", json={"action": "poweroff"}).status_code == 403


def test_no_origin_with_valid_token_is_allowed(appliance, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_UPDATE_TOKEN", "s3cret")
    with _bare(**{"X-Appliance-Token": "s3cret"}) as c:
        assert c.post("/api/appliance/update", json={"action": "reboot"}).status_code == 200
    with _bare(**{"X-Appliance-Token": "wrong"}) as c:
        assert c.post("/api/appliance/update", json={"action": "reboot"}).status_code == 403
