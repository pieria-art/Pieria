"""ConnectionManager — targeted WebSocket fan-out grouped by display_id.

Unit tests exercise connect/disconnect/send/broadcast against fake sockets; one integration test
drives the real /ws/{display_id} endpoint through TestClient to prove a sent frame is broadcast
back. The manager is a module-global singleton, so active_connections is reset around each test.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
from app import ConnectionManager, app, manager
from database import Base


@pytest.fixture(autouse=True)
def clean_manager():
    manager.active_connections.clear()
    yield
    manager.active_connections.clear()


class FakeWS:
    """Minimal async stand-in for a Starlette WebSocket."""
    def __init__(self, fail_send=False):
        self.accepted = False
        self.sent = []
        self.fail_send = fail_send

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        if self.fail_send:
            raise RuntimeError("socket gone")
        self.sent.append(message)


# ---- unit -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_accepts_and_registers():
    mgr = ConnectionManager()
    ws = FakeWS()
    await mgr.connect(ws, "wall")
    assert ws.accepted
    assert mgr.active_connections["wall"] == [ws]


@pytest.mark.asyncio
async def test_disconnect_prunes_empty_display_key():
    mgr = ConnectionManager()
    ws = FakeWS()
    await mgr.connect(ws, "wall")
    mgr.disconnect(ws, "wall")
    assert "wall" not in mgr.active_connections   # last socket gone → key removed entirely


@pytest.mark.asyncio
async def test_disconnect_keeps_other_sockets_on_same_display():
    mgr = ConnectionManager()
    a, b = FakeWS(), FakeWS()
    await mgr.connect(a, "wall")
    await mgr.connect(b, "wall")
    mgr.disconnect(a, "wall")
    assert mgr.active_connections["wall"] == [b]


@pytest.mark.asyncio
async def test_personal_message_targets_only_that_display():
    mgr = ConnectionManager()
    wall, kitchen = FakeWS(), FakeWS()
    await mgr.connect(wall, "wall")
    await mgr.connect(kitchen, "kitchen")
    await mgr.send_personal_message({"cmd": "reload"}, "wall")
    assert wall.sent == [{"cmd": "reload"}]
    assert kitchen.sent == []                     # untargeted display untouched


@pytest.mark.asyncio
async def test_broadcast_reaches_every_display():
    mgr = ConnectionManager()
    wall, kitchen = FakeWS(), FakeWS()
    await mgr.connect(wall, "wall")
    await mgr.connect(kitchen, "kitchen")
    await mgr.broadcast({"cmd": "refresh"})
    assert wall.sent == [{"cmd": "refresh"}]
    assert kitchen.sent == [{"cmd": "refresh"}]


@pytest.mark.asyncio
async def test_send_swallows_dead_socket():
    mgr = ConnectionManager()
    dead, live = FakeWS(fail_send=True), FakeWS()
    await mgr.connect(dead, "wall")
    await mgr.connect(live, "wall")
    await mgr.broadcast({"cmd": "refresh"})        # dead socket raises; must not abort the fan-out
    assert live.sent == [{"cmd": "refresh"}]


# ---- integration ----------------------------------------------------------------

def test_ws_endpoint_broadcasts_received_frame(monkeypatch):
    # Point the endpoint's heartbeat/command-poller at a throwaway in-memory DB so the live
    # /ws handler never writes to the real artwork.db.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(app_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False))

    with TestClient(app) as c:
        with c.websocket_connect("/ws/testdisp") as ws:
            assert "testdisp" in manager.active_connections
            ws.send_json({"action": "reload"})
            assert ws.receive_json() == {"action": "reload"}   # broadcast echoes back to the sender
