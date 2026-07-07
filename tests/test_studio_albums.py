"""S2: personal albums can be deleted from the Studio (is_personal playlists only; the default
'My Photos' bucket and Museum collections are protected). Photos survive — only the grouping is removed.
Rename is covered by test_collection_rename (shared PATCH /playlists/{id})."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import PERSONAL_PLAYLIST_NAME, app
from database import Base, get_db
from models import PlaylistModel


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


def _album(db, name, personal=True):
    p = PlaylistModel(name=name, is_personal=personal); db.add(p); db.commit(); db.refresh(p)
    return p


def test_delete_personal_album(client):
    c, db = client
    p = _album(db, "Summer Trip")
    r = c.delete(f"/api/studio/albums/{p.id}")
    assert r.status_code == 200
    assert db.query(PlaylistModel).filter(PlaylistModel.id == p.id).first() is None


def test_cannot_delete_default_album(client):
    c, db = client
    p = _album(db, PERSONAL_PLAYLIST_NAME)
    r = c.delete(f"/api/studio/albums/{p.id}")
    assert r.status_code == 400
    assert db.query(PlaylistModel).filter(PlaylistModel.id == p.id).first() is not None


def test_cannot_delete_museum_collection_via_studio(client):
    c, db = client
    p = _album(db, "Impressionism", personal=False)   # not is_personal
    r = c.delete(f"/api/studio/albums/{p.id}")
    assert r.status_code == 404
    assert db.query(PlaylistModel).filter(PlaylistModel.id == p.id).first() is not None


def test_delete_unknown_album_404(client):
    c, _ = client
    assert c.delete("/api/studio/albums/999999").status_code == 404
