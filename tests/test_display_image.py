"""The resolution-capped Canvas display image (/artworks/{id}/display.jpg).

Museum originals can be 150+ MP / 100 MB — too big for a Pi-class browser to paint.
The Canvas loads a capped derivative instead; the full-res original stays on disk.
"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
import core.media as core_media
import routers.display as routers_display
from app import DISPLAY_MAX_EDGE, app
from database import Base, get_db
from models import ArtworkModel


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Redirect the library + derivatives cache into a throwaway dir so tests never
    # read from or write into the real shipped Artwork/ tree (test isolation).
    monkeypatch.setattr(app_module, "LIBRARY_DIR", tmp_path / "_Library")
    monkeypatch.setattr(app_module, "DERIVATIVES_DIR", tmp_path / "_derivatives")
    # GET /artworks/{id}/display.jpg now lives in routers/display.py — it reads its own LIBRARY_DIR
    # binding, so redirect it too (established dual-patch pattern; see test_catalog.py).
    monkeypatch.setattr(routers_display, "LIBRARY_DIR", tmp_path / "_Library")
    # render_canvas_image now lives in core.media and reads core.media.DERIVATIVES_DIR;
    # patch it to the same throwaway dir so the derivative write stays isolated.
    monkeypatch.setattr(core_media, "DERIVATIVES_DIR", tmp_path / "_derivatives")

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c, db
    app.dependency_overrides.clear()
    db.close()


@pytest.fixture
def make_artwork(client):
    """Write a real image into the (redirected) library and register it. The whole
    library + derivatives cache lives under tmp_path, so no cleanup is needed."""
    c, db = client

    def _make(name, size, color=(120, 80, 40)):
        app_module.LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        path = app_module.LIBRARY_DIR / name
        Image.new("RGB", size, color).save(path, format="JPEG", quality=85)
        art = ArtworkModel(filename=name, title="t", status="approved")
        db.add(art); db.commit(); db.refresh(art)
        return c, art

    return _make


def test_oversized_image_is_capped(make_artwork):
    # A landscape original well beyond the cap (and beyond the 8192 GPU texture ceiling).
    c, art = make_artwork("_test_big_landscape.jpg", (12000, 8000))
    r = c.get(f"/artworks/{art.id}/display.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    w, h = Image.open(io.BytesIO(r.content)).size
    assert max(w, h) == DISPLAY_MAX_EDGE          # long edge pinned to the cap
    assert (w, h) == (DISPLAY_MAX_EDGE, 5120)     # aspect ratio preserved (12000:8000 = 3:2)


def test_portrait_image_is_capped_on_long_edge(make_artwork):
    c, art = make_artwork("_test_big_portrait.jpg", (8000, 12000))
    r = c.get(f"/artworks/{art.id}/display.jpg")
    w, h = Image.open(io.BytesIO(r.content)).size
    assert max(w, h) == DISPLAY_MAX_EDGE
    assert h > w                                  # still portrait


def test_small_image_passes_through(make_artwork):
    # Already under the cap → not upscaled.
    c, art = make_artwork("_test_small.jpg", (2400, 1600))
    r = c.get(f"/artworks/{art.id}/display.jpg")
    assert r.status_code == 200
    w, h = Image.open(io.BytesIO(r.content)).size
    assert (w, h) == (2400, 1600)


def test_derivative_is_cached_on_disk(make_artwork):
    c, art = make_artwork("_test_cache.jpg", (9000, 6000))
    for d in app_module.DERIVATIVES_DIR.glob(f"{art.id}-*"):   # ignore leftovers from other tests (shared dir, id reused)
        d.unlink(missing_ok=True)
    c.get(f"/artworks/{art.id}/display.jpg")
    assert list(app_module.DERIVATIVES_DIR.glob(f"{art.id}-*"))   # written through to disk


def test_display_404_for_missing_artwork(client):
    c, _ = client
    assert c.get("/artworks/999999/display.jpg").status_code == 404
