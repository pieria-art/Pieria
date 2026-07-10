"""Bulk multi-select endpoints (Inc 4): bulk link/unlink to a playlist + bulk delete from library."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
import routers.library as routers_library
from app import app
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
    # The bulk delete route (_wipe_artwork) now lives in routers/library.py — it reads its own
    # LIBRARY_DIR binding, so redirect it too (established dual-patch pattern; see test_catalog.py).
    monkeypatch.setattr(routers_library, "LIBRARY_DIR", tmp_path)

    with TestClient(app) as c:
        yield c, db, tmp_path
    app.dependency_overrides.clear()
    db.close()


def _art(db, tmp_path, name):
    """Create an artwork row plus a real file on disk (so delete can unlink it)."""
    (tmp_path / name).write_bytes(b"img")
    a = ArtworkModel(filename=name, original_width=10, original_height=10, status="approved")
    db.add(a); db.commit(); db.refresh(a)
    return a


def _members(db, pid):
    return sorted(r[0] for r in db.execute(
        select(playlist_artwork.c.artwork_id).where(playlist_artwork.c.playlist_id == pid)).all())


def test_bulk_link_adds_many_and_is_idempotent(client):
    c, db, tmp = client
    pl = PlaylistModel(name="Wall"); db.add(pl); db.commit(); db.refresh(pl)
    a, b, d = _art(db, tmp, "a.jpg"), _art(db, tmp, "b.jpg"), _art(db, tmp, "d.jpg")

    r = c.post(f"/playlists/{pl.id}/artworks", json={"artwork_ids": [a.id, b.id, d.id]})
    assert r.status_code == 200 and r.json()["count"] == 3
    assert _members(db, pl.id) == sorted([a.id, b.id, d.id])

    # re-linking the same ids must not duplicate rows (idempotent helper)
    c.post(f"/playlists/{pl.id}/artworks", json={"artwork_ids": [a.id, b.id]})
    assert _members(db, pl.id) == sorted([a.id, b.id, d.id])


def test_bulk_unlink_removes_only_listed(client):
    c, db, tmp = client
    pl = PlaylistModel(name="Wall"); db.add(pl); db.commit(); db.refresh(pl)
    a, b, d = _art(db, tmp, "a.jpg"), _art(db, tmp, "b.jpg"), _art(db, tmp, "d.jpg")
    c.post(f"/playlists/{pl.id}/artworks", json={"artwork_ids": [a.id, b.id, d.id]})

    r = c.request("DELETE", f"/playlists/{pl.id}/artworks", json={"artwork_ids": [b.id, d.id]})
    assert r.status_code == 200 and r.json()["count"] == 2
    assert _members(db, pl.id) == [a.id]
    # the artworks themselves still exist in the library
    assert db.get(ArtworkModel, b.id) is not None


def test_bulk_delete_wipes_files_and_rows(client):
    c, db, tmp = client
    a, b = _art(db, tmp, "a.jpg"), _art(db, tmp, "b.jpg")
    assert (tmp / "a.jpg").exists() and (tmp / "b.jpg").exists()

    r = c.post("/artworks/delete", json={"artwork_ids": [a.id, b.id, 99999]})  # unknown id ignored
    assert r.status_code == 200 and r.json()["count"] == 2
    assert db.get(ArtworkModel, a.id) is None and db.get(ArtworkModel, b.id) is None
    assert not (tmp / "a.jpg").exists() and not (tmp / "b.jpg").exists()


def test_bulk_delete_also_clears_playlist_membership(client):
    c, db, tmp = client
    pl = PlaylistModel(name="Wall"); db.add(pl); db.commit(); db.refresh(pl)
    a = _art(db, tmp, "a.jpg")
    c.post(f"/playlists/{pl.id}/artworks", json={"artwork_ids": [a.id]})
    assert _members(db, pl.id) == [a.id]

    c.post("/artworks/delete", json={"artwork_ids": [a.id]})
    assert _members(db, pl.id) == []   # association gone with the artwork


def test_bulk_endpoints_tolerate_empty_lists(client):
    c, db, tmp = client
    pl = PlaylistModel(name="Wall"); db.add(pl); db.commit(); db.refresh(pl)
    assert c.post(f"/playlists/{pl.id}/artworks", json={"artwork_ids": []}).json()["count"] == 0
    assert c.request("DELETE", f"/playlists/{pl.id}/artworks", json={"artwork_ids": []}).json()["count"] == 0
    assert c.post("/artworks/delete", json={"artwork_ids": []}).json()["count"] == 0


def _pending(db, tmp_path, name):
    """A Review-Queue item: an artwork row with status='pending_review'."""
    (tmp_path / name).write_bytes(b"img")
    a = ArtworkModel(filename=name, original_width=10, original_height=10, status="pending_review")
    db.add(a); db.commit(); db.refresh(a)
    return a


def test_bulk_approve_publishes_only_pending(client):
    c, db, tmp = client
    p1, p2 = _pending(db, tmp, "p1.jpg"), _pending(db, tmp, "p2.jpg")
    already = _art(db, tmp, "ok.jpg")  # status='approved' — must be left untouched / not double-counted

    r = c.post("/artworks/approve-bulk", json={"artwork_ids": [p1.id, p2.id, already.id, 99999]})
    assert r.status_code == 200 and r.json()["count"] == 2  # only the two pending ones flipped
    db.refresh(p1); db.refresh(p2); db.refresh(already)
    assert p1.status == "approved" and p2.status == "approved"
    assert already.status == "approved"  # was already approved; unchanged


def test_bulk_approve_does_not_touch_in_flight(client):
    c, db, tmp = client
    (tmp / "x.jpg").write_bytes(b"img")
    proc = ArtworkModel(filename="x.jpg", original_width=10, original_height=10, status="processing")
    db.add(proc); db.commit(); db.refresh(proc)

    r = c.post("/artworks/approve-bulk", json={"artwork_ids": [proc.id]})
    assert r.json()["count"] == 0
    db.refresh(proc); assert proc.status == "processing"  # still enriching — not force-published


def test_bulk_approve_tolerates_empty_list(client):
    c, db, tmp = client
    assert c.post("/artworks/approve-bulk", json={"artwork_ids": []}).json()["count"] == 0


def test_list_playlists_hides_underscore_pseudo_collections(client):
    """The /playlists API must never surface "_"-prefixed pseudo-collections (e.g. _derivatives, the
    optimized-image cache) — they're internal, not real collections. Keeps the rows/data, hides them
    from every consumer of the endpoint (sidebar, picker, Canvas fallback)."""
    c, db, tmp = client
    db.add(PlaylistModel(name="Summer"))
    db.add(PlaylistModel(name="_derivatives"))
    db.commit()

    names = [p["name"] for p in c.get("/playlists").json()]
    assert "Summer" in names
    assert "_derivatives" not in names
    # The row still exists in the DB — we hide, we don't delete.
    assert db.query(PlaylistModel).filter(PlaylistModel.name == "_derivatives").first() is not None
