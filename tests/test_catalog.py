"""
Catalog endpoint tests — split manifest (index + per-collection) + lazy add.

A temp catalog dir with controlled fixtures is wired in via app.CATALOG_DIR; the remote image
download is mocked; library/playlist writes go to a tmp dir. No network is touched.
"""

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
import core.downloads as core_downloads
import routers.settings as routers_settings
from app import app
from database import Base, get_db
from models import ArtworkModel, PlaylistModel, playlist_artwork

ITEM_A = {
    "title": "Test Sunrise", "agent_name": "A. Painter", "agent_role": "Painter",
    "creation_date": "1872", "date_display": "1872", "medium": "Oil on canvas",
    "cultural_context": "Impressionism", "description_narrative": "A test placard.", "tags": "sea, dawn",
    "source": "Test Museum", "license": "Public Domain",
    "source_url": "https://example.test/a/full.jpg", "thumbnail_url": "https://example.test/a/thumb.jpg",
}
ITEM_B = dict(ITEM_A, title="Test Dusk", source_url="https://example.test/b/full.jpg",
              thumbnail_url="https://example.test/b/thumb.jpg", focal_point=[0.25, 0.75])


@pytest.fixture
def client(monkeypatch, tmp_path):
    # In-memory DB
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db

    # Temp split catalog fixtures
    cat = tmp_path / "catalog"
    cat.mkdir()
    (cat / "index.json").write_text(json.dumps({"version": 1, "collections": [
        {"id": "demo", "title": "Demo", "description": "d", "source": "Test Museum",
         "license": "Public Domain", "count": 2, "cover_thumbnail": ITEM_A["thumbnail_url"]},
    ]}))
    (cat / "demo.json").write_text(json.dumps({
        "id": "demo", "title": "Demo", "description": "d", "source": "Test Museum",
        "license": "Public Domain", "items": [ITEM_A, ITEM_B],
    }))
    monkeypatch.setattr(app_module, "CATALOG_DIR", cat)

    # Redirect library/playlist writes
    monkeypatch.setattr(app_module, "LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(core_downloads, "LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(app_module, "ARTWORK_ROOT", tmp_path)

    # Mock the high-res download with a real tiny PNG
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 20, 30)).save(buf, format="PNG")
    png = buf.getvalue()

    class _Resp:
        status_code = 200
        content = png

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **k): return _Resp()

    monkeypatch.setattr(app_module.httpx, "AsyncClient", _Client)

    with TestClient(app) as c:
        yield c, db
    app.dependency_overrides.clear()
    db.close()


def test_index_returns_collection_summaries(client):
    c, _ = client
    r = c.get("/api/catalog")
    assert r.status_code == 200
    data = r.json()
    assert [col["id"] for col in data["collections"]] == ["demo"]
    assert data["collections"][0]["count"] == 2
    assert "cover_thumbnail" in data["collections"][0]
    # Index must NOT inline items (that's the per-collection endpoint).
    assert "items" not in data["collections"][0]


def test_collection_returns_items_with_added_flag(client):
    c, _ = client
    r = c.get("/api/catalog/demo")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert all(it["added"] is False for it in items)


def test_unknown_collection_404(client):
    c, _ = client
    assert c.get("/api/catalog/nope").status_code == 404


def test_add_creates_artwork_and_flips_added(client):
    c, db = client
    r = c.post("/api/catalog/add", json={"collection_id": "demo", "item_index": 0})
    assert r.status_code == 200, r.text
    art = db.query(ArtworkModel).filter(ArtworkModel.source_url == ITEM_A["source_url"]).first()
    assert art is not None and art.status == "approved" and art.title == "Test Sunrise"
    assert art.original_width == 40 and art.original_height == 30
    # added flag now true for item 0
    items = c.get("/api/catalog/demo").json()["items"]
    assert items[0]["added"] is True and items[1]["added"] is False


def test_add_is_idempotent(client):
    c, db = client
    c.post("/api/catalog/add", json={"collection_id": "demo", "item_index": 0})
    c.post("/api/catalog/add", json={"collection_id": "demo", "item_index": 0})
    assert db.query(ArtworkModel).count() == 1


def test_add_with_playlist_links_it(client):
    c, db = client
    pl = PlaylistModel(name="Wall")
    db.add(pl); db.commit(); db.refresh(pl)
    r = c.post("/api/catalog/add", json={"collection_id": "demo", "item_index": 1, "playlist_id": pl.id})
    assert r.status_code == 200, r.text
    links = db.execute(playlist_artwork.select().where(playlist_artwork.c.playlist_id == pl.id)).all()
    assert len(links) == 1


def test_add_unknown_index_404(client):
    c, _ = client
    assert c.post("/api/catalog/add", json={"collection_id": "demo", "item_index": 99}).status_code == 404


# --- Bulk catalog add (multi-select Add from the curated grid) ---

def test_bulk_add_creates_many_and_flips_added(client):
    c, db = client
    r = c.post("/api/catalog/add-bulk", json={"items": [
        {"collection_id": "demo", "item_index": 0},
        {"collection_id": "demo", "item_index": 1},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 2 and r.json()["failed"] == 0
    assert db.query(ArtworkModel).count() == 2
    items = c.get("/api/catalog/demo").json()["items"]
    assert items[0]["added"] is True and items[1]["added"] is True


def test_bulk_add_links_all_to_playlist(client):
    c, db = client
    pl = PlaylistModel(name="Wall"); db.add(pl); db.commit(); db.refresh(pl)
    r = c.post("/api/catalog/add-bulk", json={"playlist_id": pl.id, "items": [
        {"collection_id": "demo", "item_index": 0},
        {"collection_id": "demo", "item_index": 1},
    ]})
    assert r.json()["added"] == 2
    links = db.execute(playlist_artwork.select().where(playlist_artwork.c.playlist_id == pl.id)).all()
    assert len(links) == 2


def test_bulk_add_is_best_effort_past_bad_items(client):
    c, db = client
    r = c.post("/api/catalog/add-bulk", json={"items": [
        {"collection_id": "demo", "item_index": 0},    # ok
        {"collection_id": "demo", "item_index": 99},   # bad index
        {"collection_id": "nope", "item_index": 0},    # unknown collection
    ]})
    assert r.json()["added"] == 1 and r.json()["failed"] == 2
    assert db.query(ArtworkModel).count() == 1


def test_bulk_add_empty_items(client):
    c, db = client
    assert c.post("/api/catalog/add-bulk", json={"items": []}).json() == {"status": "done", "added": 0, "failed": 0}
    assert db.query(ArtworkModel).count() == 0


def test_bulk_add_idempotent_no_duplicate_rows(client):
    c, db = client
    payload = {"items": [{"collection_id": "demo", "item_index": 0}]}
    c.post("/api/catalog/add-bulk", json=payload)
    c.post("/api/catalog/add-bulk", json=payload)
    assert db.query(ArtworkModel).count() == 1   # dedup on source_url


# --- Search autocomplete (suggest) ---

def test_suggest_returns_titles(client):
    c, _ = client
    sugg = c.get("/api/catalog/suggest", params={"q": "test"}).json()["suggestions"]
    assert "Test Sunrise" in sugg and "Test Dusk" in sugg


def test_suggest_matches_artist_and_dedupes(client):
    c, _ = client
    # Both items share artist "A. Painter" → suggested once, matched on substring "paint".
    sugg = c.get("/api/catalog/suggest", params={"q": "paint"}).json()["suggestions"]
    assert sugg.count("A. Painter") == 1


def test_suggest_short_query_is_empty(client):
    c, _ = client
    assert c.get("/api/catalog/suggest", params={"q": "t"}).json()["suggestions"] == []


def test_suggest_route_not_shadowed_by_collection_id(client):
    # Declared before /api/catalog/{collection_id}, so "suggest" isn't treated as a collection (404).
    c, _ = client
    assert c.get("/api/catalog/suggest", params={"q": "sun"}).status_code == 200


# --- Flat curated search (Museum Art unified search box) ---

def test_search_finds_by_title_and_tags_collection_and_index(client):
    c, _ = client
    r = c.get("/api/catalog/search", params={"q": "sunrise"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    hit = data["results"][0]
    # Each hit must carry the coordinates the add-path needs, unchanged.
    assert hit["title"] == "Test Sunrise"
    assert hit["collection_id"] == "demo" and hit["item_index"] == 0
    assert hit["collection_title"] == "Demo" and hit["added"] is False


def test_search_matches_artist_and_is_and_across_tokens(client):
    c, _ = client
    # "painter" (artist) AND "sunrise" (title) both live in ITEM_A's haystack → 1 hit.
    assert c.get("/api/catalog/search", params={"q": "painter sunrise"}).json()["count"] == 1
    # No single item contains both "sunrise" and "dusk" → AND semantics yield nothing.
    assert c.get("/api/catalog/search", params={"q": "sunrise dusk"}).json()["count"] == 0
    # A shared token ("test" is in both titles) returns both.
    assert c.get("/api/catalog/search", params={"q": "test"}).json()["count"] == 2


def test_search_empty_query_returns_empty(client):
    c, _ = client
    assert c.get("/api/catalog/search", params={"q": "   "}).json()["results"] == []


def test_search_route_not_shadowed_by_collection_id(client):
    # Defined before /api/catalog/{collection_id}, so "search" isn't treated as a collection (404).
    c, _ = client
    assert c.get("/api/catalog/search", params={"q": "sunrise"}).status_code == 200


def test_search_reflects_added_flag(client):
    c, _ = client
    c.post("/api/catalog/add", json={"collection_id": "demo", "item_index": 0})
    hits = {h["title"]: h["added"] for h in c.get("/api/catalog/search", params={"q": "test"}).json()["results"]}
    assert hits == {"Test Sunrise": True, "Test Dusk": False}


# --- Remote catalog source (Settings → Catalog Source) ---

def test_catalog_source_defaults_to_bundled(client):
    c, _ = client
    r = c.get("/api/settings/catalog")
    assert r.status_code == 200
    assert r.json() == {"catalog_url": "", "using_remote": False}


def test_catalog_source_rejects_non_http(client):
    c, _ = client
    r = c.post("/api/settings/catalog", json={"catalog_url": "ftp://example.test/catalog"})
    assert r.status_code == 400


def test_catalog_source_saves_and_reports_count(client, monkeypatch):
    c, _ = client

    async def _fake_fetch(base, name):
        assert name == "index.json"
        return {"version": 1, "collections": [{"id": "x"}, {"id": "y"}]}
    monkeypatch.setattr(app_module, "_fetch_remote_json", _fake_fetch)
    # save_catalog_source's own validation fetch now runs in routers/settings.py — patch there too.
    monkeypatch.setattr(routers_settings, "_fetch_remote_json", _fake_fetch)

    r = c.post("/api/settings/catalog", json={"catalog_url": "https://cdn.test/catalog/"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["using_remote"] is True
    assert data["catalog_url"] == "https://cdn.test/catalog"  # trailing slash trimmed
    assert "2 collection" in data["message"]
    # persisted + reflected by GET
    g = c.get("/api/settings/catalog").json()
    assert g == {"catalog_url": "https://cdn.test/catalog", "using_remote": True}


def test_catalog_source_saves_with_warning_when_unreachable(client, monkeypatch):
    c, _ = client

    async def _boom(base, name):
        raise RuntimeError("HTTP 503")
    monkeypatch.setattr(app_module, "_fetch_remote_json", _boom)
    # save_catalog_source's own validation fetch now runs in routers/settings.py — patch there too.
    monkeypatch.setattr(routers_settings, "_fetch_remote_json", _boom)

    r = c.post("/api/settings/catalog", json={"catalog_url": "https://down.test/cat"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["using_remote"] is True and "warning" in data
    # still persisted despite being unreachable now (runtime falls back to bundled)
    assert c.get("/api/settings/catalog").json()["catalog_url"] == "https://down.test/cat"


def test_catalog_source_clear_reverts_to_bundled(client, monkeypatch):
    c, _ = client

    async def _fake_fetch(base, name):
        return {"version": 1, "collections": [{"id": "x"}]}
    monkeypatch.setattr(app_module, "_fetch_remote_json", _fake_fetch)
    # save_catalog_source's own validation fetch now runs in routers/settings.py — patch there too.
    monkeypatch.setattr(routers_settings, "_fetch_remote_json", _fake_fetch)

    c.post("/api/settings/catalog", json={"catalog_url": "https://cdn.test/catalog"})
    r = c.post("/api/settings/catalog", json={"catalog_url": ""})
    assert r.status_code == 200 and r.json()["using_remote"] is False
    assert c.get("/api/settings/catalog").json() == {"catalog_url": "", "using_remote": False}


def test_remote_index_serves_over_bundled(client, monkeypatch):
    """With a catalog_url set, /api/catalog fetches the remote index instead of the bundled one."""
    c, _ = client

    async def _fake_fetch(base, name):
        if name == "index.json":
            return {"version": 1, "collections": [{"id": "remote-col", "title": "Remote", "count": 1}]}
        raise RuntimeError("only index fetched in this test")
    monkeypatch.setattr(app_module, "_fetch_remote_json", _fake_fetch)
    # save_catalog_source's own validation fetch now runs in routers/settings.py — patch there too.
    monkeypatch.setattr(routers_settings, "_fetch_remote_json", _fake_fetch)

    c.post("/api/settings/catalog", json={"catalog_url": "https://cdn.test/catalog"})
    ids = [col["id"] for col in c.get("/api/catalog").json()["collections"]]
    assert "remote-col" in ids and "demo" not in ids  # remote replaced bundled


# --- Focal point (increment ①) ---

def test_add_copies_focal_point(client):
    c, db = client
    r = c.post("/api/catalog/add", json={"collection_id": "demo", "item_index": 1})  # ITEM_B
    assert r.status_code == 200, r.text
    art = db.query(ArtworkModel).filter(ArtworkModel.source_url == ITEM_B["source_url"]).first()
    assert art is not None and art.focal_x == 0.25 and art.focal_y == 0.75


def test_add_without_focal_defaults_centered(client):
    c, db = client
    c.post("/api/catalog/add", json={"collection_id": "demo", "item_index": 0})  # ITEM_A, no focal
    art = db.query(ArtworkModel).filter(ArtworkModel.source_url == ITEM_A["source_url"]).first()
    assert art is not None and art.focal_x == 0.5 and art.focal_y == 0.5


def test_crop_patch_persists_crop_and_focal(client):
    c, db = client
    art = ArtworkModel(filename="x.jpg", original_width=100, original_height=100, status="approved")
    db.add(art); db.commit(); db.refresh(art)
    r = c.patch(f"/artworks/{art.id}/crop", json={
        "crop_x": 10, "crop_y": 20, "crop_width": 50, "crop_height": 60,
        "focal_x": 0.3, "focal_y": 0.8})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["crop_width"] == 50 and data["focal_x"] == 0.3 and data["focal_y"] == 0.8
    db.refresh(art)
    assert art.crop_width == 50 and art.focal_x == 0.3 and art.focal_y == 0.8


def test_crop_patch_clamps_focal_and_leaves_omitted_untouched(client):
    c, db = client
    art = ArtworkModel(filename="y.jpg", original_width=100, original_height=100, status="approved")
    db.add(art); db.commit(); db.refresh(art)
    # focal_x out of range → clamped to 1.0; focal_y omitted → stays at the 0.5 default.
    r = c.patch(f"/artworks/{art.id}/crop",
                json={"crop_x": 0, "crop_y": 0, "crop_width": 10, "crop_height": 10, "focal_x": 1.7})
    assert r.status_code == 200, r.text
    db.refresh(art)
    assert art.focal_x == 1.0 and art.focal_y == 0.5


def test_crop_patch_unknown_artwork_404(client):
    c, _ = client
    r = c.patch("/artworks/9999/crop", json={"crop_x": 0, "crop_y": 0, "crop_width": 1, "crop_height": 1})
    assert r.status_code == 404
