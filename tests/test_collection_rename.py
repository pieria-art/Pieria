"""A4: collections (playlists) can be renamed via PATCH /playlists/{id}, with a collision guard and a
rejection of empty / internal underscore names."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import app
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


def _pl(db, name):
    p = PlaylistModel(name=name); db.add(p); db.commit(); db.refresh(p)
    return p


def test_rename_collection(client):
    c, db = client
    p = _pl(db, "Summre")
    r = c.patch(f"/playlists/{p.id}", json={"name": "Summer"})
    assert r.status_code == 200
    assert r.json()["name"] == "Summer"
    db.refresh(p); assert p.name == "Summer"


def test_rename_collision_rejected(client):
    c, db = client
    _pl(db, "Masterpieces")
    p = _pl(db, "Summer")
    r = c.patch(f"/playlists/{p.id}", json={"name": "Masterpieces"})
    assert r.status_code == 400
    db.refresh(p); assert p.name == "Summer"   # unchanged


def test_rename_empty_or_internal_rejected(client):
    c, db = client
    p = _pl(db, "Summer")
    assert c.patch(f"/playlists/{p.id}", json={"name": "   "}).status_code == 400
    assert c.patch(f"/playlists/{p.id}", json={"name": "_hidden"}).status_code == 400


def test_patch_without_name_still_updates_settings(client):
    c, db = client
    p = _pl(db, "Summer")
    r = c.patch(f"/playlists/{p.id}", json={"shuffle": True})
    assert r.status_code == 200
    db.refresh(p); assert p.shuffle is True and p.name == "Summer"
