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
from core.playback import MAX_KNOWN_WINDOW_SEC
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


def test_next_image_metadata_carries_series(client):
    """The kiosk placard payload exposes `series` (rendered as the title subtitle in app.js)."""
    c, db = client
    pl = PlaylistModel(name="Ukiyo", shuffle=False)
    db.add(pl); db.commit(); db.refresh(pl)
    art = ArtworkModel(filename="wave.jpg", status="approved", title="The Great Wave",
                       agent_name="Hokusai", series="Thirty-six Views of Mount Fuji")
    db.add(art); db.commit(); db.refresh(art)
    db.execute(playlist_artwork.insert().values(playlist_id=pl.id, artwork_id=art.id, display_order=0))
    db.commit()

    served = c.get("/next-image", params={"playlist_name": "Ukiyo", "display_id": "wall"}).json()
    assert served["metadata"]["series"] == "Thirty-six Views of Mount Fuji"


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


# --- The widened listing window (R2-F5a groundwork: the phone carries the e-ink placard) ----------
# /api/remote/displays lists displays the Remote should still REMEMBER, which is a longer window than
# "live": an e-ink panel pulls one frame then deep-sleeps for display_time, and a strict 15s window hid
# exactly the display whose placard the phone is meant to show. Window = max(15s, 2 x display_time),
# capped. _playlist_with_art leaves display_time at its default 30 => a 60s window, so these age by 45s
# ("asleep") and 180s ("forgotten") and never by 60s, which is the boundary itself.

def _age(db, display_id, seconds):
    row = _row(db, display_id)
    row.last_seen_at = datetime.now(UTC) - timedelta(seconds=seconds)
    db.commit()


def test_remote_displays_lists_sleeping_display_within_two_display_times(client):
    """The e-ink case: asleep between pulls, still listed, still carrying what it last showed."""
    c, db = client
    _playlist_with_art(db, "Summer", 1)
    c.get("/next-image", params={"playlist_name": "Summer", "display_id": "eink"})
    _age(db, "eink", 45)                               # past 15s live, inside the 60s known window

    displays = c.get("/api/remote/displays").json()
    assert len(displays) == 1
    assert displays[0]["live"] is False                # asleep — the remote greys out its commands
    assert displays[0]["artwork"]["title"] == "Starry Night"
    assert displays[0]["playlist"] == "Summer"


def test_remote_displays_drops_display_past_the_widened_window(client):
    c, db = client
    _playlist_with_art(db, "Summer", 1)
    c.get("/next-image", params={"playlist_name": "Summer", "display_id": "eink"})
    _age(db, "eink", 180)                              # well past 2 x display_time

    assert c.get("/api/remote/displays").json() == []


def test_remote_displays_live_true_for_fresh_display(client):
    c, db = client
    _playlist_with_art(db, "Summer", 1)
    c.get("/next-image", params={"playlist_name": "Summer", "display_id": "wall"})

    assert c.get("/api/remote/displays").json()[0]["live"] is True


def test_remote_displays_window_falls_back_to_live_without_a_playlist(client):
    """No current_playlist (never served, or the playlist was renamed out from under the denormalized
    name) => the join misses and the window collapses to the 15s live window."""
    c, db = client
    db.add(ActiveDisplayModel(display_id="stale", last_seen_at=datetime.now(UTC) - timedelta(seconds=20)))
    db.add(ActiveDisplayModel(display_id="fresh", last_seen_at=datetime.now(UTC) - timedelta(seconds=5)))
    db.commit()

    ids = [d["display_id"] for d in c.get("/api/remote/displays").json()]
    assert ids == ["fresh"]


def test_remote_displays_window_is_capped(client):
    """touch_active_display rows are never GC'd, so an absurd display_time must not park an unplugged
    panel in the dropdown for days."""
    c, db = client
    pl = PlaylistModel(name="Glacial", display_time=999999)
    db.add(pl); db.commit()
    art = ArtworkModel(filename="slow.jpg", status="approved", title="Slow", agent_name="Someone")
    db.add(art); db.commit(); db.refresh(art)
    db.execute(playlist_artwork.insert().values(playlist_id=pl.id, artwork_id=art.id, display_order=0))
    db.commit()
    c.get("/next-image", params={"playlist_name": "Glacial", "display_id": "eink"})
    _age(db, "eink", MAX_KNOWN_WINDOW_SEC + 60)

    assert c.get("/api/remote/displays").json() == []


def test_next_image_metadata_key_set_is_the_placard_contract(client):
    """Guards the placard_metadata() extraction: /next-image's metadata block is a published contract
    (app.js updatePlacard, MMM-Pieria, and now /artworks/{id}/placard all read it)."""
    c, db = client
    _playlist_with_art(db, "Summer", 1)
    served = c.get("/next-image", params={"playlist_name": "Summer", "display_id": "wall"}).json()

    assert set(served["metadata"].keys()) == {
        "id", "is_personal", "title", "agent_name", "agent_role", "creation_date",
        "cultural_context", "medium", "date_display", "series", "description", "tags",
    }
