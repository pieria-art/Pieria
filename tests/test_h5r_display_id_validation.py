"""H5r — display_id validation on /ws and /api/remote/change (residual of H5).

H5 fixed the WS broadcast-to-all and the CORS/CSWSH gaps but left display_id itself unvalidated on
these two endpoints. Both now use appliance_settings.validate_display_id_runtime — a runtime-only
validator that does NOT depend on sd-conf being importable (unlike validate("DISPLAY_ID", ...), which
fails closed and would gate the WebSocket on an unrelated conf-writer availability check). It admits
the ids real displays already use (help.html documents `/?display=<name>` with any string, and
hand-edited DISPLAY_ID values), refusing only control characters, '/', '\\', empty, and >64 chars.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

import app as app_module
import routers.ws as routers_ws
from app import app, manager
from core import appliance_settings
from database import Base, get_db


@pytest.fixture(autouse=True)
def clean_manager():
    manager.active_connections.clear()
    yield
    manager.active_connections.clear()


@pytest.fixture
def client(monkeypatch):
    # Isolated in-memory DB (established dual-patch pattern; see test_connection_manager.py) so these
    # tests never write RemoteCommandModel/ActiveDisplayModel rows into the real artwork.db.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(app_module, "SessionLocal", session_factory)
    monkeypatch.setattr(routers_ws, "SessionLocal", session_factory)

    def _override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# --- /ws/{display_id} --------------------------------------------------------

def test_ws_rejects_invalid_display_id_with_policy_violation(client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/Bad Display!"):
            pass
    assert exc_info.value.code == 1008
    assert "Bad Display!" not in manager.active_connections


def test_ws_accepts_valid_display_id(client):
    with client.websocket_connect("/ws/wstestdisp") as ws:
        assert "wstestdisp" in manager.active_connections
        ws.send_json({"action": "reload"})
        assert ws.receive_json() == {"action": "reload"}


# --- /api/remote/change ------------------------------------------------------

def test_remote_change_rejects_invalid_display_id(client):
    resp = client.post("/api/remote/change", json={"target_display": "../etc/passwd", "action": "next"})
    assert resp.status_code == 400


def test_remote_change_accepts_valid_display_id(client):
    resp = client.post("/api/remote/change", json={"target_display": "remotetestdisp", "action": "next"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "command_queued"


# --- validate_display_id_runtime unit coverage --------------------------------

@pytest.mark.parametrize("value", [
    "Kitchen", "LivingRoom", "tv.1", "hall tv", "_x", "docent-living-room", "default", "magicmirror",
])
def test_validate_display_id_runtime_accepts(value):
    assert appliance_settings.validate_display_id_runtime(value) is None


@pytest.mark.parametrize("value", [
    "bad\x00id", "bad\nid", "../x", "a" * 65, "",
])
def test_validate_display_id_runtime_refuses(value):
    assert appliance_settings.validate_display_id_runtime(value) is not None


def test_validate_display_id_runtime_does_not_depend_on_sd_conf(monkeypatch):
    """Must never fail closed just because sd-conf isn't importable — that's the whole point of a
    separate validator (the WebSocket should not be gated on the appliance conf-writer)."""
    monkeypatch.setattr(appliance_settings, "sd_conf", None)
    assert appliance_settings.validate_display_id_runtime("hall tv") is None
