"""M6 nit (reviewer, 2026-09-25): CorsAndOriginGuardMiddleware (app.py, ADR-036) must judge the FIRST
Origin header when a request carries more than one — matching Starlette's own Request.headers.get()
behavior (what the old BaseHTTPMiddleware version read) rather than silently preferring the last copy,
which a naive `{k: v for k, v in headers}` dict comprehension over the raw ASGI header list would do.
"""

import pytest

import config
from app import CorsAndOriginGuardMiddleware


async def _run(app_wrapped, headers: list[tuple[bytes, bytes]], method: str = "GET", path: str = "/api/x"):
    scope = {"type": "http", "method": method, "path": path, "headers": headers}
    events = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        events.append(message)

    await app_wrapped(scope, receive, send)
    return events


@pytest.mark.asyncio
async def test_duplicated_origin_header_uses_the_first_one(monkeypatch):
    # Configure the SECOND origin as trusted and the FIRST as not — if the middleware picked the
    # last header (old dict-comprehension bug), this untrusted cross-origin POST would be allowed.
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", ["https://trusted.example"])

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = CorsAndOriginGuardMiddleware(downstream)

    events = await _run(
        mw,
        headers=[
            (b"origin", b"https://untrusted.example"),
            (b"origin", b"https://trusted.example"),
            (b"host", b"app.example"),
        ],
        method="POST",
    )

    start = next(e for e in events if e["type"] == "http.response.start")
    assert start["status"] == 403, (
        "the untrusted FIRST Origin header should have been judged (and refused) — "
        "picking the trusted LAST one instead would wrongly let this through"
    )


@pytest.mark.asyncio
async def test_single_origin_header_still_works(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", ["https://trusted.example"])

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = CorsAndOriginGuardMiddleware(downstream)
    events = await _run(
        mw,
        headers=[(b"origin", b"https://trusted.example"), (b"host", b"app.example")],
        method="POST",
    )
    start = next(e for e in events if e["type"] == "http.response.start")
    assert start["status"] == 200
    headers = dict(start["headers"])
    assert headers.get(b"access-control-allow-origin") == b"https://trusted.example"
