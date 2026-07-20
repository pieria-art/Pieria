"""The Track B e-ink pull endpoint (/display/{display_id}/current.{ext}).

This is the whole low-power surface: an e-ink frame wakes, GETs one URL, paints, and deep-sleeps for
`X-Refresh-After` seconds. It had no test coverage at all, despite being the only render path a dumb
client can reach — so a regression here is invisible until it shows up on glass at the bench.
"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
import routers.display as routers_display
from app import app
from database import Base, get_db
from models import ArtworkModel, PlaylistModel


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Same dual-patch isolation as test_display_image.py: the route reads its own LIBRARY_DIR binding.
    monkeypatch.setattr(app_module, "LIBRARY_DIR", tmp_path / "_Library")
    monkeypatch.setattr(routers_display, "LIBRARY_DIR", tmp_path / "_Library")

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
def seeded(client):
    """One approved artwork in one playlist — the minimum a pull needs to resolve."""
    c, db = client
    routers_display.LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    # a non-square source so any aspect handling is actually exercised
    Image.new("RGB", (900, 1200), (150, 90, 40)).save(
        routers_display.LIBRARY_DIR / "_pull.jpg", format="JPEG", quality=85
    )
    art = ArtworkModel(filename="_pull.jpg", title="t", status="approved",
                       focal_x=0.42, focal_y=0.25)
    db.add(art); db.commit(); db.refresh(art)
    pl = PlaylistModel(name="default")
    pl.artworks.append(art)
    db.add(pl); db.commit()
    return c, db, art


def test_pull_returns_exact_requested_size(seeded):
    c, _, _ = seeded
    r = c.get("/display/eink1/current.png?w=1600&h=1200")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (1600, 1200)


def test_pull_honours_portrait_dimensions(seeded):
    """A portrait panel must get a portrait composition, not a rotated landscape one."""
    c, _, _ = seeded
    r = c.get("/display/eink1/current.png?w=1200&h=1600")
    assert r.status_code == 200
    assert Image.open(io.BytesIO(r.content)).size == (1200, 1600)


def test_pull_sends_sleep_and_change_detection_headers(seeded):
    """The client deep-sleeps on X-Refresh-After and skips a ~9s repaint when the ETag is unchanged."""
    c, _, _ = seeded
    r = c.get("/display/eink1/current.png")
    assert r.status_code == 200
    assert int(r.headers["X-Refresh-After"]) > 0
    assert r.headers["ETag"]
    assert "no-store" in r.headers["Cache-Control"]
    # same artwork, same params -> same bytes -> same ETag, so the panel doesn't needlessly refresh
    assert c.get("/display/eink1/current.png").headers["ETag"] == r.headers["ETag"]


def test_pull_rejects_unknown_extension_and_palette(seeded):
    c, _, _ = seeded
    assert c.get("/display/eink1/current.gif").status_code == 404
    assert c.get("/display/eink1/current.png?palette=nope").status_code == 400


def test_pull_404s_when_no_playlist_exists(client):
    c, _ = client
    assert c.get("/display/eink1/current.png").status_code == 404


def test_pull_404s_when_the_file_is_missing(seeded):
    """A DB row whose file vanished must 404, not 500 — the client backs off instead of hard-failing."""
    c, _, _ = seeded
    (routers_display.LIBRARY_DIR / "_pull.jpg").unlink()
    assert c.get("/display/eink1/current.png").status_code == 404


def test_pull_bmp_format(seeded):
    c, _, _ = seeded
    r = c.get("/display/eink1/current.bmp?w=200&h=150")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/bmp"
    assert Image.open(io.BytesIO(r.content)).size == (200, 150)
