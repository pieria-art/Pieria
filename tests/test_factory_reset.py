"""Factory-reset smoke — /api/admin/factory-reset wipes the box back to bare metal.

Both seed and non-seed artworks and their on-disk files are removed (seeds re-download on next
boot), every playlist association is cleared, and the discovery queue is emptied. This is a
destructive endpoint; the test drives it against a fully throwaway Artwork/ tree and DB.
"""

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
from app import app
from database import Base, get_db
from models import (
    ArtworkModel,
    DiscoveryQueueModel,
    PlaylistModel,
    playlist_artwork,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    art_root = tmp_path / "art"
    lib = art_root / "_Library"
    lib.mkdir(parents=True)
    monkeypatch.setattr(app_module, "ARTWORK_ROOT", art_root)
    monkeypatch.setattr(app_module, "LIBRARY_DIR", lib)
    monkeypatch.setattr(app_module, "DERIVATIVES_DIR", art_root / "_derivatives")

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


def _art(db, name, is_seed):
    path = app_module.LIBRARY_DIR / name
    Image.new("RGB", (8, 8)).save(path, "JPEG")
    art = ArtworkModel(filename=name, status="approved", is_seed=is_seed)
    db.add(art); db.commit(); db.refresh(art)
    return art, path


def test_factory_reset_wipes_db_files_and_queue(client):
    c, db = client
    pl = PlaylistModel(name="Wall"); db.add(pl); db.commit(); db.refresh(pl)
    seed, seed_path = _art(db, "seed.jpg", is_seed=True)
    user, user_path = _art(db, "user.jpg", is_seed=False)
    for art in (seed, user):
        db.execute(playlist_artwork.insert().values(playlist_id=pl.id, artwork_id=art.id, display_order=0))
    db.add(DiscoveryQueueModel(source_url="http://x/a.jpg", thumbnail_url="http://x/t.jpg", source_api="test"))
    db.commit()

    # H4: a bare POST with no confirmation is refused server-side.
    assert c.post("/api/admin/factory-reset", json={}).status_code == 400
    assert db.query(ArtworkModel).count() == 2   # nothing wiped by the rejected call

    r = c.post("/api/admin/factory-reset", json={"confirm": "RESET"})
    assert r.status_code == 200
    body = r.json()
    assert body["artworks_removed"] == 1             # the one non-seed
    assert body["seed_artworks_removed"] == 1
    assert body["files_deleted"] == 1                # non-seed file count (seed files removed in a later pass)
    assert body["queue_items_cleared"] == 1

    # DB fully cleared of artworks + associations + queue.
    assert db.query(ArtworkModel).count() == 0
    assert db.query(playlist_artwork).count() == 0
    assert db.query(DiscoveryQueueModel).count() == 0
    # Files gone from disk (both seed and non-seed).
    assert not seed_path.exists()
    assert not user_path.exists()
