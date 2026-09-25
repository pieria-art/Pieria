"""M6 (INFRA-D075 #2, 2026-09-25): app.py's two `@app.middleware("http")` layers
(inject_aggressive_cache_headers, cors_and_origin_guard) were BaseHTTPMiddleware, which runs the
downstream app in a SEPARATE anyio task from the one the ASGI server cancels on a client disconnect.
That's a documented leak vector independent of any one endpoint: a disconnect mid-request can leave the
inner task's `finally` blocks — including a `Depends(get_db)` session's own `db.close()` — never run,
so the connection it checked out of the pool is never returned. Converting both to pure ASGI
(CacheHeadersMiddleware / CorsAndOriginGuardMiddleware) runs in the SAME task as the request, so a
disconnect propagates straight through the endpoint's own exception handling — same as no middleware
at all — and the dependency's cleanup still runs.

This builds a throwaway app that wires the two real middleware classes the same way app.py does, around
a slow Depends(get_db) route, against a real QueuePool-backed sqlite engine — then cancels a request
mid-flight (simulating a client disconnect) and asserts the pool comes back to empty.
"""

import asyncio

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app import CacheHeadersMiddleware, CorsAndOriginGuardMiddleware


def _build_app(engine):
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    test_app = FastAPI()
    # Same add_middleware order/wrapping as app.py's real stack.
    test_app.add_middleware(CacheHeadersMiddleware)
    test_app.add_middleware(CorsAndOriginGuardMiddleware)

    @test_app.get("/slow")
    async def slow(seconds: float = 2.0, db=Depends(get_db)):
        db.execute(text("SELECT 1"))   # checks a connection out of the pool for this session
        await asyncio.sleep(seconds)
        return {"ok": True}

    return test_app, engine


@pytest.fixture
def pooled_app(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'leak.db'}",
        poolclass=QueuePool, pool_size=1, max_overflow=0,
        connect_args={"check_same_thread": False},
    )
    test_app, _ = _build_app(engine)
    try:
        yield test_app, engine
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_client_disconnect_does_not_leak_pool_connection(pooled_app):
    test_app, engine = pooled_app
    transport = httpx.ASGITransport(app=test_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Simulate a client disconnect mid-request: cancel the in-flight request well before its
        # 2s sleep finishes (this is exactly the admin-grid-tab-closed / timeout scenario from prod).
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(client.get("/slow", params={"seconds": 2}), timeout=0.1)

        # Give any cleanup a brief moment, then the pool MUST be back to empty — a leak here means
        # every later request (even an unrelated one) queues forever behind a connection nobody
        # returned, which is exactly what took the prod box down until a container restart.
        for _ in range(40):
            if engine.pool.checkedout() == 0:
                break
            await asyncio.sleep(0.05)
        assert engine.pool.checkedout() == 0

        # And with pool_size=1, a fresh request only succeeds if the pool actually has its one
        # connection back — this is the "does the app recover" half of the incident.
        r = await client.get("/slow", params={"seconds": 0})
        assert r.status_code == 200
