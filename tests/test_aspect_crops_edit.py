"""Admin two-tier crop UI, server side: PATCH /artworks/{id}/crop accepts per-shape `aspect_crops`
(validated, merged, marked user-edited per ADR-148 F8) and `reset_aspect_crops` releases the mark."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import app
from core.artwork_edits import edited_fields
from database import Base, get_db
from models import ArtworkModel
from tests import test_pack_refresh as pr


@pytest.fixture
def ctx():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _db():
        yield db
    app.dependency_overrides[get_db] = _db
    yield TestClient(app), db
    app.dependency_overrides.pop(get_db, None)


def _art(db, w=1600, h=1000):
    a = ArtworkModel(filename="x.jpg", original_width=w, original_height=h, status="approved")
    db.add(a); db.commit(); db.refresh(a)
    return a


def _patch(c, a, **kw):
    return c.patch(f"/artworks/{a.id}/crop", json={"crop_width": 0, "crop_height": 0, **kw})


def _box_916(w=1600, h=1000, hh=0.9, x0=0.2):
    """A 9:16 box in PIXELS on a w x h image, as normalized coordinates."""
    bw = (hh * h * 9 / 16) / w
    return [x0, 0.05, round(x0 + bw, 5), round(0.05 + hh, 5)]


def test_valid_box_persists_and_is_marked(ctx):
    c, db = ctx
    a = _art(db)
    box = _box_916()
    r = _patch(c, a, aspect_crops={"9:16": box})
    assert r.status_code == 200, r.text
    assert r.json()["aspect_crops"] == {"9:16": box}
    db.refresh(a)
    assert a.aspect_crops == {"9:16": box}
    assert "aspect_crops_json" in edited_fields(a)


def test_merge_keeps_other_shapes(ctx):
    c, db = ctx
    a = _art(db)
    a.aspect_crops_json = json.dumps({"16:9": [0.0, 0.1, 1.0, 0.9]}); db.commit()
    _patch(c, a, aspect_crops={"9:16": _box_916()})
    db.refresh(a)
    assert set(a.aspect_crops) == {"16:9", "9:16"}


@pytest.mark.parametrize("crops", [
    {"5:4": [0, 0, 0.5, 0.5]},                         # unknown shape
    {"9:16": [0.2, 0.05, 0.9, 1.2]},                   # out of bounds
    {"9:16": [0.5, 0.1, 0.4, 0.9]},                    # inverted
    {"9:16": [0.2, 0.05, 0.25, 0.9]},                  # wrong ratio
    {"16:9": _box_916()},                              # right box, wrong shape key
    {"9:16": "nope"},
    {"9:16": [0.1, 0.1, "a", 0.5]},
])
def test_bad_boxes_422(ctx, crops):
    c, db = ctx
    a = _art(db)
    r = _patch(c, a, aspect_crops=crops)
    assert r.status_code == 422, r.text
    db.refresh(a)
    assert a.aspect_crops is None and "aspect_crops_json" not in edited_fields(a)


def test_too_small_box_422(ctx):
    c, db = ctx
    a = _art(db, w=1000, h=1000)
    r = _patch(c, a, aspect_crops={"4:3": [0.1, 0.1, 0.18, 0.1 + 0.08 * 0.75]})
    assert r.status_code == 422 and "small" in r.text


def test_reset_releases_the_mark_and_stays_released(ctx):
    c, db = ctx
    a = _art(db)
    _patch(c, a, aspect_crops={"9:16": _box_916()}, focal_x=0.3, focal_y=0.4)
    db.refresh(a)
    assert {"aspect_crops_json", "focal_x"} <= edited_fields(a)
    _patch(c, a, reset_aspect_crops=True, focal_x=0.3, focal_y=0.4)
    db.refresh(a)
    assert "aspect_crops_json" not in edited_fields(a)
    assert "focal_x" in edited_fields(a)
    _patch(c, a, focal_x=0.3, focal_y=0.4)   # re-saving an unchanged focal must not re-freeze the framing
    db.refresh(a)
    assert "aspect_crops_json" not in edited_fields(a)


def test_pack_refresh_keeps_edited_framing_and_updates_after_reset(tmp_path, monkeypatch):
    root, priv = pr._setup(tmp_path, monkeypatch, None)
    pr._publish(root, priv, pr._v1_items())
    db = pr._db()
    pr._install(db, root)
    a = pr._art(db, "alpha")
    a.original_width = a.original_height = 1000
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    try:
        c = TestClient(app)
        mine = [0.2, 0.0, 0.7625, 1.0]
        assert _patch(c, a, aspect_crops={"9:16": mine}).status_code == 200
        pack = {"9:16": [0.0, 0.0, 0.5625, 1.0]}
        items = [pr._mi("Alpha", filename="alpha.jpg", source_url="https://x/Alpha.jpg", aspect_crops=pack),
                 pr._mi("Beta"), pr._mi("Gamma")]
        pr._publish(root, priv, items)
        pr._install(db, root)
        db.refresh(a)
        assert a.aspect_crops == {"9:16": mine}                  # the user's framing survives the refresh
        _patch(c, a, reset_aspect_crops=True)
        pr._publish(root, priv, items)
        pr._install(db, root)
        db.refresh(a)
        assert a.aspect_crops == pack                            # released -> the pack may update it
    finally:
        app.dependency_overrides.pop(get_db, None)
