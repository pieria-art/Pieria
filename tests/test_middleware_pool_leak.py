"""M6 (INFRA-D075, 2026-09-25) regression: the thumbnail-storm production incident.

An earlier version of this file blamed BaseHTTPMiddleware for leaking pool connections on client
disconnect — a reviewer's load test showed that ISN'T what happened on this FastAPI 0.141 / Starlette
0.52 stack (BaseHTTPMiddleware did not leak here). The REAL mechanism: `get_artwork_thumbnail` (and
`/preview`, `/display.jpg`) ran a synchronous `db.query(...)` directly inside `async def`, not threaded.
SQLAlchemy's QueuePool checkout is a blocking call (`threading.Condition.wait`) — when the pool is
exhausted, that call blocks the calling thread until a connection frees up or `pool_timeout` fires. Run
directly on the event-loop thread, it blocks the WHOLE LOOP, including every other coroutine — in
particular the very continuations (post-render session close) that would return a connection to the
pool. Once the pool is exhausted, the loop can only make progress in `pool_timeout`-sized bursts, and
under sustained concurrent load (an admin grid opening ~100 thumbnails) it never catches up: every
request, even an unrelated one like `/playlists`, stalls behind the wedged loop.

The fix (8adea72) moved the DB lookup into `run_in_threadpool` (`core.media.lookup_artwork_filename`)
so the blocking pool-checkout wait happens on a worker thread, never the loop thread — plus closes the
session before the (also threaded, semaphore-bounded) Pillow render, so the connection is barely held
at all. The pure-ASGI middleware conversion from the same commit is kept (it's correct and faster) but
is NOT what fixes this bug — see app.py's CacheHeadersMiddleware/CorsAndOriginGuardMiddleware docstring.

This test fires a burst of concurrent thumbnail requests against a real, small QueuePool-backed engine
(so pool exhaustion is real, not simulated) with the image render slowed down, and asserts (a) an
unrelated fast DB route stays responsive DURING the storm and (b) the whole storm drains promptly.
Verified by hand (INFRA-D075 review) to FAIL against 8adea72^ (e2bd494) and PASS at/after 8adea72.
"""

import threading
import time

import PIL.Image as PILImage
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

import app as app_module
import core.media as core_media
import database
import routers.library as routers_library
from app import app
from database import Base
from models import ArtworkModel, PlaylistModel

# Small enough that a modest burst exhausts it (prod: pool_size=5 + overflow=10, exhausted by ~100
# concurrent thumbnails); short pool_timeout so a genuinely wedged loop fails this test in seconds,
# not the real 30s default.
_POOL_SIZE = 2
_POOL_TIMEOUT = 2
_STORM_SIZE = 20
_RENDER_DELAY_S = 0.3


@pytest.fixture
def storm_client(tmp_path, monkeypatch):
    library = tmp_path / "_Library"
    library.mkdir()
    Image.new("RGB", (300, 200), (10, 20, 30)).save(library / "a.jpg", format="JPEG")
    monkeypatch.setattr(app_module, "LIBRARY_DIR", library)
    monkeypatch.setattr(routers_library, "LIBRARY_DIR", library)
    monkeypatch.setattr(core_media, "DERIVATIVES_DIR", tmp_path / "_derivatives")

    engine = create_engine(
        f"sqlite:///{tmp_path / 'pool.db'}",
        poolclass=QueuePool, pool_size=_POOL_SIZE, max_overflow=0, pool_timeout=_POOL_TIMEOUT,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    seed = session_factory()
    art = ArtworkModel(filename="a.jpg", status="approved")
    seed.add(art)
    seed.add(PlaylistModel(name="p"))
    seed.commit()
    seed.refresh(art)
    art_id = art.id
    seed.close()

    # Route the REAL Depends(get_db) machinery at this small-pool engine — no dependency_overrides,
    # because we want genuine pool contention, not a single shared test session. Dual-patch every
    # module that binds its own SessionLocal (established pattern; see test_connection_manager.py).
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    # raising=False: these two bindings only exist post-fix (8adea72) — harmless no-op pre-fix, where
    # the route goes through Depends(get_db) / database.SessionLocal directly instead.
    monkeypatch.setattr(core_media, "SessionLocal", session_factory, raising=False)
    monkeypatch.setattr(routers_library, "SessionLocal", session_factory, raising=False)

    with TestClient(app) as c:
        yield c, art_id
    engine.dispose()


def test_thumbnail_storm_does_not_wedge_the_event_loop(storm_client, monkeypatch):
    c, art_id = storm_client

    # Slow down every Pillow decode (patched on the shared PIL.Image module, so it applies no matter
    # which module's `get_optimized_image` binding actually runs Image.open — old code and new code
    # import it under different names).
    real_open = PILImage.open

    def _slow_open(*a, **k):
        time.sleep(_RENDER_DELAY_S)
        return real_open(*a, **k)

    monkeypatch.setattr(PILImage, "open", _slow_open)

    results = {}

    def _fire(i):
        try:
            r = c.get(f"/artworks/{art_id}/thumbnail")
            results[i] = r.status_code
        except Exception as e:  # noqa: BLE001 — captured for the assertion, not swallowed
            results[i] = f"error:{type(e).__name__}: {e}"

    threads = [threading.Thread(target=_fire, args=(i,)) for i in range(_STORM_SIZE)]
    t0 = time.monotonic()
    for t in threads:
        t.start()

    # Let the storm get going, then probe a totally unrelated, fast DB route — this is the actual prod
    # symptom (every request, even /api/demo, timed out until the container was restarted).
    time.sleep(0.5)
    probe_t0 = time.monotonic()
    control = c.get("/playlists")
    probe_elapsed = time.monotonic() - probe_t0
    assert control.status_code == 200
    assert probe_elapsed < _POOL_TIMEOUT * 1.5, (
        f"/playlists took {probe_elapsed:.1f}s during the thumbnail storm — the event loop was "
        "blocked by a synchronous pool-checkout wait on another request's coroutine"
    )

    for t in threads:
        t.join(timeout=30)
    drain_elapsed = time.monotonic() - t0
    assert all(not t.is_alive() for t in threads), "thumbnail storm threads never finished — deadlock"
    assert drain_elapsed < 15, f"thumbnail storm took {drain_elapsed:.1f}s to drain (expected a few seconds)"
    assert all(v == 200 for v in results.values()), results
