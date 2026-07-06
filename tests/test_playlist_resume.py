"""Reboot playlist resume + the display-cache warm sweep.

On reboot (no ?playlist=) a display should resume what it was last showing, fall back to a configured
default, then to the first non-empty — never silently snap to playlist #1.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import app
from database import Base, get_db
from models import ArtworkModel, PlaylistModel, SettingsModel, playlist_artwork


@pytest.fixture
def client():
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


def _playlist(db, name, with_art=True):
    pl = PlaylistModel(name=name)
    db.add(pl); db.commit(); db.refresh(pl)
    if with_art:
        art = ArtworkModel(filename=f"{name}.jpg", status="approved")
        db.add(art); db.commit(); db.refresh(art)
        db.execute(playlist_artwork.insert().values(playlist_id=pl.id, artwork_id=art.id, display_order=0))
        db.commit()
    return pl


# ---- preferred-playlist resolver -------------------------------------------------

def test_preferred_falls_back_to_null_when_no_history(client):
    c, db = client
    _playlist(db, "Masterpieces")
    r = c.get("/api/displays/wall/preferred-playlist")
    assert r.status_code == 200
    assert r.json()["playlist"] is None   # no history, no default → Canvas picks first non-empty itself


def test_preferred_resumes_last_played(client):
    c, db = client
    _playlist(db, "Masterpieces")
    _playlist(db, "Summer")
    db.add(SettingsModel(setting_key="last_playlist:wall", setting_value="Summer")); db.commit()
    assert c.get("/api/displays/wall/preferred-playlist").json()["playlist"] == "Summer"


def test_last_played_beats_default(client):
    c, db = client
    _playlist(db, "Masterpieces")
    _playlist(db, "Summer")
    db.add(SettingsModel(setting_key="last_playlist:wall", setting_value="Summer"))
    db.add(SettingsModel(setting_key="default_playlist", setting_value="Masterpieces")); db.commit()
    assert c.get("/api/displays/wall/preferred-playlist").json()["playlist"] == "Summer"


def test_default_used_when_no_history(client):
    c, db = client
    _playlist(db, "Masterpieces")
    _playlist(db, "Summer")
    db.add(SettingsModel(setting_key="default_playlist", setting_value="Summer")); db.commit()
    assert c.get("/api/displays/wall/preferred-playlist").json()["playlist"] == "Summer"


def test_preferred_skips_deleted_or_empty_playlist(client):
    c, db = client
    _playlist(db, "Empty", with_art=False)            # exists but no art
    db.add(SettingsModel(setting_key="last_playlist:wall", setting_value="Empty"))
    db.add(SettingsModel(setting_key="default_playlist", setting_value="Ghost")); db.commit()  # nonexistent
    assert c.get("/api/displays/wall/preferred-playlist").json()["playlist"] is None


# ---- default-playlist setting endpoint -------------------------------------------

def test_set_and_get_default_playlist(client):
    c, db = client
    _playlist(db, "Summer")
    assert c.post("/api/settings/default-playlist", json={"default_playlist": "Summer"}).status_code == 200
    assert c.get("/api/settings/default-playlist").json()["default_playlist"] == "Summer"


def test_set_default_rejects_unknown_playlist(client):
    c, _ = client
    assert c.post("/api/settings/default-playlist", json={"default_playlist": "Nope"}).status_code == 400


def test_clear_default_playlist(client):
    c, db = client
    _playlist(db, "Summer")
    c.post("/api/settings/default-playlist", json={"default_playlist": "Summer"})
    c.post("/api/settings/default-playlist", json={"default_playlist": ""})
    assert c.get("/api/settings/default-playlist").json()["default_playlist"] == ""


# ---- last-played recorded by /next-image -----------------------------------------

def test_next_image_records_last_playlist(client):
    c, db = client
    _playlist(db, "Summer")
    c.get("/next-image", params={"playlist_name": "Summer", "display_id": "wall"})
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == "last_playlist:wall").first()
    assert row is not None and row.setting_value == "Summer"


# ---- filesystem sync ignores internal dirs (regression: _derivatives-as-collection) ----

def test_sync_ignores_underscore_dirs(client, tmp_path, monkeypatch):
    """An underscore-prefixed internal dir (e.g. the _derivatives display cache) must never become a
    collection or have its .jpg files absorbed into the library as artworks."""
    from PIL import Image

    import app as app_module
    from app import sync_db_with_filesystem
    c, db = client
    # Redirect the artwork tree into tmp_path so the sync scan never touches the real Artwork/ dir.
    monkeypatch.setattr(app_module, "ARTWORK_ROOT", tmp_path / "art")
    monkeypatch.setattr(app_module, "LIBRARY_DIR", tmp_path / "art" / "_Library")
    cache = app_module.ARTWORK_ROOT / "_derivtest"
    cache.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (10, 10)).save(cache / "5-123-7680.jpg", "JPEG")
    sync_db_with_filesystem(db)
    assert "_derivtest" not in [p.name for p in db.query(PlaylistModel).all()]      # no bogus collection
    assert db.query(ArtworkModel).filter(ArtworkModel.filename == "5-123-7680.jpg").first() is None  # not absorbed
    assert (cache / "5-123-7680.jpg").exists()                                       # left in place
