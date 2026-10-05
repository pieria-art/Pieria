"""ADR-148 follow-up: run the real short-session helpers against a real temp SQLite FILE with NO shared
session. The autouse conftest fixture makes router `SessionLocal`s share ONE session with a no-op close,
which hides "forgot to commit" and "returned an ORM object after the session closed" bugs. Here each
`SessionLocal()` is a genuinely independent session, and we assert committed state is visible from a fresh
session and that responses are plain data.
"""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routers.curation as routers_curation
import routers.library as routers_library
import routers.publisher as routers_publisher
import routers.settings as routers_settings
from app import app
from database import Base, get_db
from models import ArtworkModel, DiscoveryQueueModel, PublisherCollectionModel, SettingsModel


@pytest.fixture
def real(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'real.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    app.dependency_overrides.pop(get_db, None)   # NO shared-session override
    for mod in (routers_library, routers_settings, routers_publisher, routers_curation):
        monkeypatch.setattr(mod, "SessionLocal", factory)   # explicit patch wins over the autouse fixture
    monkeypatch.setattr(routers_library, "LIBRARY_DIR", tmp_path)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(routers_library, "run_ai_pipeline", _noop)
    monkeypatch.setattr(routers_curation, "run_rag_pipeline", _noop)
    yield TestClient(app), factory, tmp_path
    engine.dispose()


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (50, 40), (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


def test_upload_insert_commits_and_returns_plain_data(real):
    c, factory, _ = real
    r = c.post("/upload", files={"file": ("pic.png", _png(), "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_review" and body["original_width"] == 50
    with factory() as fresh:
        art = fresh.get(ArtworkModel, body["id"])
        assert art is not None and art.filename == body["filename"]


def test_approve_insert_commits_both_rows(real, monkeypatch):
    c, factory, tmp = real
    with factory() as s:
        item = DiscoveryQueueModel(source_url="https://x.test/a.jpg", thumbnail_url="https://t/a.jpg",
                                   proposed_title="Piece", proposed_artist="A", source_api="x", status="pending")
        s.add(item); s.commit(); item_id = item.id

    async def _fake_dl(url, *, filename, retries=3):
        p = tmp / (filename + ".jpg")
        p.write_bytes(b"img")
        return p, p.name, 40, 30
    monkeypatch.setattr(routers_curation, "_download_image_to_library", _fake_dl)
    r = c.post(f"/api/discover/approve/{item_id}")
    assert r.status_code == 200, r.text
    with factory() as fresh:
        art = fresh.get(ArtworkModel, r.json()["artwork_id"])
        assert art.status == "processing" and art.source_url == "https://x.test/a.jpg"
        assert fresh.get(DiscoveryQueueModel, item_id).status == "approved"


def test_approve_when_row_removed_during_download_is_409_and_cleans_up(real, monkeypatch):
    c, factory, tmp = real
    with factory() as s:
        item = DiscoveryQueueModel(source_url="https://x.test/b.jpg", thumbnail_url="https://t/b.jpg",
                                   proposed_title="Gone", proposed_artist="A", source_api="x", status="pending")
        s.add(item); s.commit(); item_id = item.id

    async def _dl_then_reject(url, *, filename, retries=3):
        p = tmp / (filename + ".jpg")
        p.write_bytes(b"img")
        with factory() as s2:   # the user rejects/clears the queue while the image is downloading
            s2.query(DiscoveryQueueModel).filter(DiscoveryQueueModel.id == item_id).delete()
            s2.commit()
        return p, p.name, 40, 30
    monkeypatch.setattr(routers_curation, "_download_image_to_library", _dl_then_reject)
    r = c.post(f"/api/discover/approve/{item_id}")
    assert r.status_code == 409
    with factory() as fresh:
        assert fresh.query(ArtworkModel).count() == 0
    assert not list(tmp.glob("scouted_*"))   # the downloaded file was removed


def test_publisher_create_update_export_use_committed_state(real):
    c, factory, _ = real
    import publisher
    priv, pub = publisher.keygen()
    with factory() as s:
        for k, v in (("publisher_id", "me"), ("publisher_name", "Me"), ("publisher_private_key", priv),
                     ("publisher_public_key", pub)):
            s.add(SettingsModel(setting_key=k, setting_value=v))
        s.commit()
    r = c.post("/api/publisher/collections", json={"title": "My Set", "items": []})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    with factory() as fresh:
        assert fresh.get(PublisherCollectionModel, cid).slug == "my-set"
    r = c.put(f"/api/publisher/collections/{cid}", json={"title": "Renamed", "slug": "renamed", "items": []})
    assert r.status_code == 200 and r.json()["slug"] == "renamed"
    with factory() as fresh:
        assert fresh.get(PublisherCollectionModel, cid).title == "Renamed"
    assert c.put("/api/publisher/collections/9999", json={"title": "x", "items": []}).status_code == 404
    # export reads identity + collection through its own short session; any non-5xx proves plain-data hand-off
    assert c.post(f"/api/publisher/collections/{cid}/export").status_code in (200, 422)  # not 400 (key found) / 5xx


def test_settings_catalog_save_and_clear_commit(real, monkeypatch):
    c, factory, _ = real

    async def _fake_fetch(base, name):
        return {"collections": [1, 2]}
    monkeypatch.setattr(routers_settings, "_fetch_remote_json", _fake_fetch)
    r = c.post("/api/settings/catalog", json={"catalog_url": "https://cat.test/"})
    assert r.status_code == 200 and r.json()["catalog_url"] == "https://cat.test"
    with factory() as fresh:
        assert fresh.query(SettingsModel).filter_by(setting_key="catalog_url").first().setting_value == "https://cat.test"
    assert c.post("/api/settings/catalog", json={"catalog_url": ""}).status_code == 200
    with factory() as fresh:
        assert fresh.query(SettingsModel).filter_by(setting_key="catalog_url").first() is None
