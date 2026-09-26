"""A real uvicorn server must be able to accept WebSockets.

Starlette's TestClient drives WebSockets in-process, so every WS test passes even when uvicorn has no
WebSocket library — in which case the running server 404s every /ws/ upgrade. That shipped in 1.0
(the fastapi 0.141 bump dropped the transitive `websockets`), and the kiosk lost its heartbeat and
remote with the whole suite green.
"""
from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol


def test_uvicorn_has_a_websocket_implementation():
    assert AutoWebSocketsProtocol is not None, (
        "uvicorn found neither `websockets` nor `wsproto` — the server will 404 every /ws/ upgrade"
    )
