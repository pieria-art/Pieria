"""Bag-shuffle + sequential selection internals of /next-image.

Bag shuffle draws without replacement until the bag empties, then refills — so every artwork is
shown once per cycle regardless of the affinity weighting on the draw order. Sequential mode
advances a per-display cursor and wraps. Both persist state in DisplayPlaybackSessionModel.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import app
from database import Base, get_db
from models import (
    ArtworkModel,
    DisplayPlaybackSessionModel,
    PlaylistModel,
    playlist_artwork,
)


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


def _playlist_with_art(db, name, n, shuffle=False):
    pl = PlaylistModel(name=name, shuffle=shuffle)
    db.add(pl); db.commit(); db.refresh(pl)
    ids = []
    for i in range(n):
        art = ArtworkModel(filename=f"{name}_{i}.jpg", status="approved")
        db.add(art); db.commit(); db.refresh(art)
        db.execute(playlist_artwork.insert().values(playlist_id=pl.id, artwork_id=art.id, display_order=i))
        ids.append(art.id)
    db.commit()
    return pl, ids


def test_bag_drains_without_repeat_then_refills(client):
    c, db = client
    _, ids = _playlist_with_art(db, "Bag", 3)
    # Three shuffle draws must cover all three artworks exactly once (no replacement within a cycle).
    drawn = [c.get("/next-image", params={"playlist_name": "Bag", "shuffle": True,
                                          "display_id": "wall"}).json()["metadata"]["id"]
             for _ in range(3)]
    assert sorted(drawn) == sorted(ids)
    assert len(set(drawn)) == 3
    # The fourth draw refills the empty bag and returns a valid member again.
    fourth = c.get("/next-image", params={"playlist_name": "Bag", "shuffle": True,
                                          "display_id": "wall"}).json()["metadata"]["id"]
    assert fourth in ids


def test_bag_filters_stale_ids_and_refills(client):
    c, db = client
    pl, ids = _playlist_with_art(db, "Stale", 1)
    # Seed the persisted bag with an id that is no longer a valid/approved member of the playlist.
    db.add(DisplayPlaybackSessionModel(display_id="wall", playlist_id=pl.id,
                                       unplayed_artworks_json="[999999]"))
    db.commit()
    r = c.get("/next-image", params={"playlist_name": "Stale", "shuffle": True, "display_id": "wall"})
    assert r.status_code == 200
    assert r.json()["metadata"]["id"] == ids[0]      # stale 999999 filtered out; bag refilled to real member


def test_sequential_advances_and_wraps(client):
    c, db = client
    _playlist_with_art(db, "Seq", 3)
    seen = [c.get("/next-image", params={"playlist_name": "Seq", "shuffle": False,
                                         "display_id": "wall", "direction": 1}).json()["index"]
            for _ in range(4)]
    assert seen == [0, 1, 2, 0]                       # advances by direction, wraps modulo count


def test_next_image_404_for_unknown_playlist(client):
    c, _ = client
    assert c.get("/next-image", params={"playlist_name": "Nope"}).status_code == 404
