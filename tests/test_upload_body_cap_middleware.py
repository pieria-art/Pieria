"""M4 — the ASGI-level body-size cap on /upload and /upload/personal (app.py:UploadBodyCapMiddleware).

core/media.py's read_capped_upload runs inside the route handler, which is too late: Starlette
parses/spools the multipart body before the handler ever runs. This middleware rejects with 413
before that parsing starts — a Content-Length pre-check, and a running byte count for
chunked/no-Content-Length bodies. These tests exercise the middleware itself, not the (still-present)
handler-level cap covered by tests/test_personal.py.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
from app import app
from database import Base, get_db


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


def test_oversized_content_length_rejected_before_handler(client, monkeypatch):
    called = False

    async def _boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("handler should not run for an oversized Content-Length")

    monkeypatch.setattr(app_module, "USER_UPLOAD_MAX_BYTES", 100)
    # The middleware reads its cap from app.py's own USER_UPLOAD_MAX_BYTES import at call time via
    # the module-level _UPLOAD_BODY_CAP constant, so patch the constant directly for this test.
    monkeypatch.setattr(app_module, "_UPLOAD_BODY_CAP", 100)
    import core.media as media
    monkeypatch.setattr(media, "read_capped_upload", _boom)

    big = b"x" * 10_000
    r = client.post("/upload/personal",
                     files={"file": ("art.png", big, "image/png")},
                     data={"caption": "c"})
    assert r.status_code == 413
    assert called is False


def test_chunked_oversized_body_rejected(client, monkeypatch):
    """No Content-Length header (a generator body forces httpx to stream it) — the middleware must
    fall back to counting bytes off the wire rather than trusting a declared size."""
    monkeypatch.setattr(app_module, "_UPLOAD_BODY_CAP", 1000)

    def _gen():
        chunk = b"x" * 2000
        yield chunk

    r = client.post("/upload/personal", content=_gen(),
                     headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413
