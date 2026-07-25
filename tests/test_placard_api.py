"""GET /artworks/{id}/placard — the placard as JSON, for the phone Remote's 'Read placard' tile.

An e-ink panel renders art ONLY: render_for_epaper fits and dithers the image and bakes no text, by
design. So the phone becomes the placard surface for it. This endpoint is what that tile reads.

The load-bearing property here is that it CANNOT drift from what the TV shows — both projections come
from core.playback.placard_metadata, and test_placard_key_set_matches_next_image_metadata makes that
executable rather than aspirational.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import app
from database import Base, get_db
from models import ArtworkModel, PlaylistModel, playlist_artwork


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


def _art(db, **kw):
    defaults = dict(filename="wave.jpg", status="approved", title="The Great Wave",
                    agent_name="Hokusai", agent_role="Artist", creation_date="c. 1831",
                    cultural_context="Japanese", medium="Woodblock print", date_display="1831",
                    series="Thirty-six Views of Mount Fuji",
                    description_narrative="A towering wave curls over three boats.",
                    tags="ukiyo-e, seascape")
    art = ArtworkModel(**{**defaults, **kw})
    db.add(art); db.commit(); db.refresh(art)
    return art


def test_placard_returns_full_metadata(client):
    c, db = client
    art = _art(db)

    p = c.get(f"/artworks/{art.id}/placard").json()
    assert p["id"] == art.id
    assert p["title"] == "The Great Wave"
    assert p["agent_name"] == "Hokusai"
    assert p["agent_role"] == "Artist"
    assert p["creation_date"] == "c. 1831"
    assert p["cultural_context"] == "Japanese"
    assert p["medium"] == "Woodblock print"
    assert p["date_display"] == "1831"
    assert p["series"] == "Thirty-six Views of Mount Fuji"
    assert p["description"] == "A towering wave curls over three boats."
    assert p["tags"] == "ukiyo-e, seascape"
    assert p["is_personal"] is False


def test_placard_strips_markdown(client):
    """The AI enrichment emits Markdown emphasis. The Canvas strips it client-side in stripMd(); the
    phone gets it stripped here instead, on the same three fields, so remote.html needs no copy."""
    c, db = client
    art = _art(db, title="*The Great Wave*", series="_Thirty-six Views_",
               description_narrative="A **towering** wave and `foam`.")

    p = c.get(f"/artworks/{art.id}/placard").json()
    assert p["title"] == "The Great Wave"
    assert p["series"] == "Thirty-six Views"
    assert p["description"] == "A towering wave and foam."


def test_placard_key_set_matches_next_image_metadata(client):
    """The anti-drift test. Add a field to the TV placard without adding it to the phone (or vice
    versa) and this fails — which is the only thing keeping the two surfaces honest over time."""
    c, db = client
    pl = PlaylistModel(name="Ukiyo")
    db.add(pl); db.commit(); db.refresh(pl)
    art = _art(db)
    db.execute(playlist_artwork.insert().values(playlist_id=pl.id, artwork_id=art.id, display_order=0))
    db.commit()

    served = c.get("/next-image", params={"playlist_name": "Ukiyo", "display_id": "wall"}).json()
    placard = c.get(f"/artworks/{art.id}/placard").json()

    assert set(placard.keys()) == set(served["metadata"].keys())


def test_placard_404_unknown_artwork(client):
    c, _ = client
    assert c.get("/artworks/999999/placard").status_code == 404


def test_placard_personal_photo_passes_through(client):
    """is_personal is reported, not acted on. /next-image doesn't blank the museum fields either — the
    client branches on the flag, and identical key sets are what the drift test above depends on."""
    c, db = client
    art = _art(db, is_personal=True, title="Beach day", agent_name="Josh")

    p = c.get(f"/artworks/{art.id}/placard").json()
    assert p["is_personal"] is True
    assert p["title"] == "Beach day"
    assert p["agent_name"] == "Josh"


def test_placard_never_enriches(client, monkeypatch):
    """Reading a placard must not trigger generation. Placards are made on upload, on admin re-enrich,
    or offline at pack-build; a null description just means one was never made."""
    def _boom(*a, **kw):
        raise AssertionError("placard read triggered enrichment")
    monkeypatch.setattr("agents.process_artwork", _boom)

    c, db = client
    art = _art(db, description_narrative=None)

    r = c.get(f"/artworks/{art.id}/placard")
    assert r.status_code == 200
    assert r.json()["description"] is None
