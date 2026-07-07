"""Tests for the Studio Personal-mode upload path (increment ③)."""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
from app import PERSONAL_PLAYLIST_NAME, app
from database import Base, get_db
from models import ArtworkModel, PlaylistModel, playlist_artwork


@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    monkeypatch.setattr(app_module, "LIBRARY_DIR", tmp_path)

    with TestClient(app) as c:
        yield c, db
    app.dependency_overrides.clear()
    db.close()


def _png_bytes(size=(60, 40), color=(10, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _upload(c, **data):
    filename = data.pop("filename", "snap.png")
    size = data.pop("size", (60, 40))
    return c.post("/upload/personal",
                  files={"file": (filename, _png_bytes(size), "image/png")},
                  data=data)


def test_personal_upload_is_approved_personal_no_review(client):
    c, db = client
    r = _upload(c, caption="Beach Day", date="2026")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_personal"] is True
    assert body["status"] == "approved"          # not pending_review — bypasses the queue
    assert body["title"] == "Beach Day"
    assert body["date_display"] == "2026"
    assert body["focal_x"] == 0.5 and body["focal_y"] == 0.5
    art = db.get(ArtworkModel, body["id"])
    assert art.filename.startswith("personal_") and art.filename.endswith(".png")


def test_personal_upload_auto_creates_my_photos_playlist(client):
    c, db = client
    body = _upload(c, caption="Pet").json()
    pl = db.query(PlaylistModel).filter(PlaylistModel.name == PERSONAL_PLAYLIST_NAME).first()
    assert pl is not None
    links = db.execute(select(playlist_artwork).where(
        playlist_artwork.c.playlist_id == pl.id,
        playlist_artwork.c.artwork_id == body["id"])).all()
    assert len(links) == 1


def test_personal_upload_respects_given_playlist(client):
    c, db = client
    pl = PlaylistModel(name="Vacation"); db.add(pl); db.commit(); db.refresh(pl)
    body = _upload(c, caption="Sunset", playlist_id=pl.id).json()
    ids = [r[0] for r in db.execute(select(playlist_artwork.c.artwork_id).where(
        playlist_artwork.c.playlist_id == pl.id)).all()]
    assert body["id"] in ids
    # did NOT also create the default "My Photos" playlist
    assert db.query(PlaylistModel).filter(PlaylistModel.name == PERSONAL_PLAYLIST_NAME).first() is None


def test_personal_upload_collision_safe_filenames(client):
    c, db = client
    a = _upload(c, caption="Dog").json()
    b = _upload(c, caption="Dog").json()
    assert db.get(ArtworkModel, a["id"]).filename != db.get(ArtworkModel, b["id"]).filename


def test_personal_upload_rejects_non_image(client):
    c, _ = client
    r = c.post("/upload/personal", files={"file": ("notimg.txt", b"hello world", "text/plain")})
    assert r.status_code == 400


# --- HEIC (the iPhone default capture format) ---

def _heic_bytes(size=(80, 60), color=(200, 100, 50)):
    """A real HEIC payload — only decodable because app.py registers the pillow-heif opener."""
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="HEIF")
    return buf.getvalue()


def test_personal_upload_heic_transcodes_to_jpeg(client):
    c, db = client
    r = c.post("/upload/personal",
               files={"file": ("IMG_2025.HEIC", _heic_bytes((80, 60)), "image/heic")},
               data={"caption": "From my iPhone"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_personal"] is True and body["status"] == "approved"
    art = db.get(ArtworkModel, body["id"])
    # Stored as a browser-renderable JPEG, not the unrenderable .heic.
    assert art.filename.endswith(".jpg")
    with Image.open(app_module.LIBRARY_DIR / art.filename) as im:
        assert im.format == "JPEG" and im.size == (80, 60)


def test_museum_upload_heic_transcodes_to_jpeg(client, monkeypatch):
    c, db = client
    monkeypatch.setattr(app_module, "run_ai_pipeline", lambda *a, **k: None)
    r = c.post("/upload", files={"file": ("photo.heic", _heic_bytes((50, 40)), "image/heic")})
    assert r.status_code == 200, r.text
    art = db.get(ArtworkModel, r.json()["id"])
    assert art.filename.endswith(".jpg") and art.status == "pending_review"
    with Image.open(app_module.LIBRARY_DIR / art.filename) as im:
        assert im.format == "JPEG" and im.size == (50, 40)


def test_museum_upload_png_preserves_bytes_with_safe_name(client, monkeypatch):
    """Non-HEIC museum path keeps the exact original bytes + format, but the on-disk NAME is now
    server-generated (C1: the client filename is never used to build the path). The stem still
    carries a sanitized hint from the original name for recognizability."""
    c, db = client
    monkeypatch.setattr(app_module, "run_ai_pipeline", lambda *a, **k: None)
    r = c.post("/upload", files={"file": ("art.png", _png_bytes((70, 50)), "image/png")})
    assert r.status_code == 200, r.text
    art = db.get(ArtworkModel, r.json()["id"])
    assert art.filename.startswith("upload_") and art.filename.endswith(".png")   # server-generated
    assert art.original_width == 70 and art.original_height == 50
    with Image.open(app_module.LIBRARY_DIR / art.filename) as im:
        assert im.format == "PNG" and im.size == (70, 50)   # bytes/format preserved


def test_museum_upload_rejects_traversal_filename(client, monkeypatch):
    """C1 regression: a client filename with path traversal cannot escape LIBRARY_DIR — the name is
    server-generated, so the artwork lands safely inside the library."""
    c, db = client
    monkeypatch.setattr(app_module, "run_ai_pipeline", lambda *a, **k: None)
    r = c.post("/upload", files={"file": ("../../../evil.png", _png_bytes((20, 20)), "image/png")})
    assert r.status_code == 200, r.text
    art = db.get(ArtworkModel, r.json()["id"])
    assert "/" not in art.filename and ".." not in art.filename
    assert (app_module.LIBRARY_DIR / art.filename).resolve().parent == app_module.LIBRARY_DIR.resolve()


# --- AI auto-caption (increment ④) ---

@pytest.mark.parametrize("url,local", [
    ("http://localhost:11434/v1", True),
    ("http://127.0.0.1:1234/v1", True),
    ("http://host.docker.internal:11434/v1", True),
    ("http://192.168.1.50:11434/v1", True),
    ("http://10.0.0.5/v1", True),
    ("https://api.openai.com/v1", False),
    ("https://generativelanguage.googleapis.com/v1beta/openai", False),
    ("https://openrouter.ai/api/v1", False),
    ("", False),
])
def test_is_local_base_url(url, local):
    import ai_client
    assert ai_client.is_local_base_url(url) is local


def test_caption_suggests_and_reports_local(client, monkeypatch):
    c, _ = client
    body = _upload(c, caption="").json()   # creates a personal artwork + its file on disk
    monkeypatch.setattr(app_module.ai_client, "get_ai_config",
                        lambda force=False: {"configured": True, "base_url": "http://localhost:11434/v1"})
    monkeypatch.setattr(app_module.ai_client, "chat",
                        lambda *a, **k: '{"caption": "A Sunny Day at Bondi Beach"}')
    r = c.post(f"/api/studio/caption/{body['id']}", json={"hint": "Bondi"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["caption"] == "A Sunny Day at Bondi Beach"
    assert d["model_is_local"] is True   # localhost model ⇒ photo stays on-device


def test_caption_reports_cloud_model(client, monkeypatch):
    c, _ = client
    body = _upload(c).json()
    monkeypatch.setattr(app_module.ai_client, "get_ai_config",
                        lambda force=False: {"configured": True, "base_url": "https://api.openai.com/v1"})
    monkeypatch.setattr(app_module.ai_client, "chat", lambda *a, **k: '{"caption": "Golden Hour"}')
    d = c.post(f"/api/studio/caption/{body['id']}", json={}).json()
    assert d["caption"] == "Golden Hour" and d["model_is_local"] is False


def test_caption_requires_configured_model(client, monkeypatch):
    c, _ = client
    body = _upload(c).json()
    monkeypatch.setattr(app_module.ai_client, "get_ai_config",
                        lambda force=False: {"configured": False, "base_url": ""})
    assert c.post(f"/api/studio/caption/{body['id']}", json={}).status_code == 400


def test_caption_unknown_artwork_404(client):
    c, _ = client
    assert c.post("/api/studio/caption/99999", json={}).status_code == 404


# --- Jargon-free placard / detail page (increment ⑤) ---

def test_art_detail_page_personal_strips_jargon(client):
    c, _ = client
    body = _upload(c, caption="Beach Day", date="2024").json()
    page = c.get(f"/art/{body['id']}")
    assert page.status_code == 200
    html = page.text
    assert "Beach Day" in html            # the caption is the title
    assert "Unknown artist" not in html   # no museum artist line
    assert "View original source" not in html


def test_next_image_metadata_carries_is_personal(client):
    c, _ = client
    # _upload auto-creates + links a "My Photos" playlist, so /next-image has something to select.
    _upload(c, caption="Pet Nap")
    info = c.get("/next-image", params={"playlist_name": PERSONAL_PLAYLIST_NAME})
    assert info.status_code == 200, info.text
    assert info.json()["metadata"]["is_personal"] is True


# --- My Photos studio: page + caption/date save (increment ⑥) ---

def test_studio_page_served(client):
    c, _ = client
    r = c.get("/studio")
    assert r.status_code == 200 and "My Photos" in r.text


def test_update_personal_photo_caption_and_date(client):
    c, _ = client
    body = _upload(c).json()
    r = c.patch(f"/api/studio/photo/{body['id']}", json={"caption": "Grandma's Garden", "date": "2025"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["title"] == "Grandma's Garden" and d["date_display"] == "2025"


def test_update_personal_photo_partial_leaves_other(client):
    c, db = client
    body = _upload(c, caption="Original", date="2020").json()
    c.patch(f"/api/studio/photo/{body['id']}", json={"caption": "Renamed"})  # date omitted
    art = db.get(ArtworkModel, body["id"])
    assert art.title == "Renamed" and art.date_display == "2020"


def test_update_rejects_non_personal(client):
    c, db = client
    art = ArtworkModel(filename="m.jpg", original_width=10, original_height=10,
                       status="approved", is_personal=False)
    db.add(art); db.commit(); db.refresh(art)
    assert c.patch(f"/api/studio/photo/{art.id}", json={"caption": "x"}).status_code == 404


# --- Studio gallery: GET /api/studio/photos (Inc 2) ---

def test_studio_photos_groups_by_album_and_excludes_museum(client):
    c, db = client
    # two personal photos in a named album, one in the default "My Photos"
    pl = PlaylistModel(name="Stuff"); db.add(pl); db.commit(); db.refresh(pl)
    _upload(c, caption="Canal", playlist_id=pl.id)
    _upload(c, caption="Bridge", playlist_id=pl.id)
    _upload(c, caption="Default One")           # auto "My Photos"
    # a museum artwork must NOT appear in the personal gallery
    db.add(ArtworkModel(filename="museum.jpg", original_width=10, original_height=10,
                        status="approved", is_personal=False, title="Mona Lisa")); db.commit()

    data = c.get("/api/studio/photos").json()
    assert data["count"] == 3
    by_name = {a["name"]: a for a in data["albums"]}
    assert set(by_name) == {"Stuff", PERSONAL_PLAYLIST_NAME}
    assert {p["title"] for p in by_name["Stuff"]["photos"]} == {"Canal", "Bridge"}
    # carries the fields the gallery card needs
    p = by_name["Stuff"]["photos"][0]
    assert {"id", "title", "date_display", "focal_x", "focal_y", "filename"} <= set(p)
    assert "Mona Lisa" not in [pp["title"] for al in data["albums"] for pp in al["photos"]]


def test_studio_photos_unfiled_group(client):
    c, db = client
    # a personal photo with no playlist link → "Unfiled"
    db.add(ArtworkModel(filename="loose.jpg", original_width=10, original_height=10,
                        status="approved", is_personal=True, title="Loose")); db.commit()
    data = c.get("/api/studio/photos").json()
    assert data["count"] == 1
    assert data["albums"][-1]["name"] == "Unfiled"
    assert data["albums"][-1]["playlist_id"] is None


def test_studio_photos_empty(client):
    c, _ = client
    assert c.get("/api/studio/photos").json() == {"albums": [], "count": 0}


# --- Edit landing: PATCH /artworks/{id}/metadata (Inc 3) ---

_META = {"title": "Edited", "agent_name": "A. Artist", "agent_role": "Painter",
         "creation_date": "1900", "cultural_context": "Modernism", "medium": "Oil",
         "date_display": "1900", "description_narrative": "A new blurb.", "tags": "a, b"}


def test_update_artwork_metadata_edits_in_place_keeps_status(client):
    c, db = client
    art = ArtworkModel(filename="m.jpg", original_width=10, original_height=10,
                       status="approved", is_personal=False, title="Old")
    db.add(art); db.commit(); db.refresh(art)
    r = c.patch(f"/artworks/{art.id}/metadata", json=_META)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Edited" and body["agent_name"] == "A. Artist"
    assert body["status"] == "approved"      # NOT bounced back to pending_review
    db.refresh(art)
    assert art.description_narrative == "A new blurb." and art.status == "approved"


def test_update_artwork_metadata_404(client):
    c, _ = client
    assert c.patch("/artworks/99999/metadata", json=_META).status_code == 404


# --- Personal albums (My Photos chips; is_personal playlists) ---------------------------------------

def test_studio_albums_lists_only_personal(client):
    """GET /api/studio/albums returns only is_personal playlists — Museum collections never appear."""
    c, db = client
    db.add(PlaylistModel(name="The Masterpieces", is_personal=False)); db.commit()
    _upload(c)  # auto-creates the "My Photos" personal default
    albums = c.get("/api/studio/albums").json()
    names = [a["name"] for a in albums]
    assert PERSONAL_PLAYLIST_NAME in names
    assert "The Masterpieces" not in names
    assert albums[0]["name"] == PERSONAL_PLAYLIST_NAME and albums[0]["is_default"] is True


def test_create_personal_album(client):
    """POST /api/studio/albums creates an is_personal playlist (empty albums list too); dedupes."""
    c, db = client
    r = c.post("/api/studio/albums", json={"name": "Vacation 2026"})
    assert r.status_code == 200, r.text
    pl = db.query(PlaylistModel).filter(PlaylistModel.name == "Vacation 2026").first()
    assert pl is not None and pl.is_personal is True
    # Empty album still appears in the chip list.
    assert "Vacation 2026" in [a["name"] for a in c.get("/api/studio/albums").json()]
    # Duplicate name rejected.
    assert c.post("/api/studio/albums", json={"name": "Vacation 2026"}).status_code == 400


def test_upload_personal_default_album_is_flagged(client):
    """The auto-created 'My Photos' album is marked is_personal so it shows in My Photos."""
    c, db = client
    _upload(c)
    pl = db.query(PlaylistModel).filter(PlaylistModel.name == PERSONAL_PLAYLIST_NAME).first()
    assert pl is not None and pl.is_personal is True
