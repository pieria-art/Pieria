"""Now-playing everywhere (R1a).

/next-image is the single selection brain (Canvas + e-ink both flow through it); it records the
artwork/collection each display is currently showing onto the active_displays row. The Remote and
Devices views read that back. Liveness (last_seen_at) stays heartbeat-owned, so a display that goes
idle keeps its last-known now-playing but drops out of the 'active' set.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import app
from database import Base, get_db
from models import ActiveDisplayModel, ArtworkModel, PlaylistModel, playlist_artwork


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


def _playlist_with_art(db, name, n, shuffle=False, title="Starry Night", agent="van Gogh"):
    pl = PlaylistModel(name=name, shuffle=shuffle)
    db.add(pl); db.commit(); db.refresh(pl)
    ids = []
    for i in range(n):
        art = ArtworkModel(filename=f"{name}_{i}.jpg", status="approved",
                           title=title, agent_name=agent)
        db.add(art); db.commit(); db.refresh(art)
        db.execute(playlist_artwork.insert().values(playlist_id=pl.id, artwork_id=art.id, display_order=i))
        ids.append(art.id)
    db.commit()
    return pl, ids


def _row(db, display_id):
    return db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == display_id).first()


def test_next_image_records_now_playing(client):
    c, db = client
    _, ids = _playlist_with_art(db, "Summer", 1)
    served = c.get("/next-image", params={"playlist_name": "Summer", "display_id": "wall"}).json()

    row = _row(db, "wall")
    assert row is not None
    assert row.current_artwork_id == served["metadata"]["id"] == ids[0]
    assert row.current_playlist == "Summer"


def test_now_playing_endpoint_returns_artwork_card(client):
    c, db = client
    _playlist_with_art(db, "Summer", 1)
    c.get("/next-image", params={"playlist_name": "Summer", "display_id": "wall"})

    np = c.get("/api/displays/wall/now-playing").json()
    assert np["display_id"] == "wall"
    assert np["active"] is True                       # the serve just created a fresh (now) row
    assert np["playlist"] == "Summer"
    art = np["artwork"]
    assert art["title"] == "Starry Night"
    assert art["agent_name"] == "van Gogh"
    assert art["is_personal"] is False
    assert art["thumb_url"] == f"/artworks/{art['id']}/thumbnail"


def test_now_playing_unknown_display(client):
    c, _ = client
    np = c.get("/api/displays/ghost/now-playing").json()
    assert np == {"display_id": "ghost", "active": False, "playlist": None, "artwork": None}


def test_now_playing_survives_going_idle(client):
    """A display that stopped heartbeating keeps its last-known now-playing but is no longer active."""
    c, db = client
    _playlist_with_art(db, "Summer", 1)
    c.get("/next-image", params={"playlist_name": "Summer", "display_id": "wall"})

    row = _row(db, "wall")
    row.last_seen_at = datetime.now(UTC) - timedelta(seconds=60)   # age it past the 15s cutoff
    db.commit()

    np = c.get("/api/displays/wall/now-playing").json()
    assert np["active"] is False
    assert np["artwork"] is not None                  # still remembers what it last showed
    assert np["playlist"] == "Summer"


def test_remote_displays_carries_now_playing_shape(client):
    c, db = client
    _playlist_with_art(db, "Summer", 1)
    c.get("/next-image", params={"playlist_name": "Summer", "display_id": "wall"})

    displays = c.get("/api/remote/displays").json()
    assert len(displays) == 1
    d = displays[0]
    assert d["display_id"] == "wall"
    assert d["playlist"] == "Summer"
    assert d["artwork"]["title"] == "Starry Night"


def test_now_playing_artwork_null_after_deletion(client):
    """If the recorded artwork is gone, the card degrades to null rather than 500-ing."""
    c, db = client
    _, ids = _playlist_with_art(db, "Summer", 1)
    c.get("/next-image", params={"playlist_name": "Summer", "display_id": "wall"})

    db.query(ArtworkModel).filter(ArtworkModel.id == ids[0]).delete()
    db.commit()

    np = c.get("/api/displays/wall/now-playing").json()
    assert np["artwork"] is None
    assert np["playlist"] == "Summer"                 # collection is still remembered


def test_next_image_updates_on_collection_change(client):
    """Switching a display to another collection overwrites its now-playing (no stale row/duplicate)."""
    c, db = client
    _playlist_with_art(db, "Summer", 1, title="Starry Night")
    _playlist_with_art(db, "Winter", 1, title="Snow Scene", agent="Bruegel")

    c.get("/next-image", params={"playlist_name": "Summer", "display_id": "wall"})
    c.get("/next-image", params={"playlist_name": "Winter", "display_id": "wall"})

    rows = db.query(ActiveDisplayModel).filter(ActiveDisplayModel.display_id == "wall").all()
    assert len(rows) == 1                              # upsert, not insert
    assert rows[0].current_playlist == "Winter"
    np = c.get("/api/displays/wall/now-playing").json()
    assert np["artwork"]["title"] == "Snow Scene"
