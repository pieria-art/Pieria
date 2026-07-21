"""Tests for the `aspect_crops` persistence plumbing (mirrors ADR-052's series/resolution_tier
pattern): the `_aspect_crops` read-path parser in core/downloads.py, and migration 0006.
"""
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

import models  # noqa: F401  — registers all tables on Base.metadata
from core.downloads import _aspect_crops
from database import Base


def test_aspect_crops_valid_boxes_pass_through():
    item = {"aspect_crops": {"16:9": [0.0, 0.1, 1.0, 0.9], "9:16": [0.2, 0.0, 0.8, 1.0]}}
    result = _aspect_crops(item)
    assert result == {"16:9": [0.0, 0.1, 1.0, 0.9], "9:16": [0.2, 0.0, 0.8, 1.0]}


def test_aspect_crops_absent_is_none():
    assert _aspect_crops({}) is None
    assert _aspect_crops({"aspect_crops": None}) is None
    assert _aspect_crops({"aspect_crops": "not-a-dict"}) is None


def test_aspect_crops_partially_malformed_is_tolerant():
    """One bad box must not void the others (read-path tolerance, unlike the derivation tool)."""
    item = {"aspect_crops": {
        "16:9": [0.0, 0.1, 1.0, 0.9],          # valid
        "9:16": [1.0, 0.0, 0.0, 1.0],          # x0 > x1: malformed
        "4:3": "nonsense",                      # not a list at all
        "3:4": [0.1, 0.1, 0.9],                 # wrong length
    }}
    result = _aspect_crops(item)
    assert result == {"16:9": [0.0, 0.1, 1.0, 0.9]}


def test_aspect_crops_full_frame_preserved():
    """normalize_crop_box() returns None for a near-full-frame box (its own "no-op" convention),
    but that's a meaningful "use the whole image" answer for a given shape, not invalid — must be
    kept as [0,0,1,1], not silently dropped like a genuinely malformed box."""
    item = {"aspect_crops": {"16:9": [0.0, 0.0, 1.0, 1.0], "4:3": [0.001, 0.0, 0.999, 1.0]}}
    result = _aspect_crops(item)
    assert result == {"16:9": [0.0, 0.0, 1.0, 1.0], "4:3": [0.0, 0.0, 1.0, 1.0]}


def test_aspect_crops_unknown_keys_dropped():
    item = {"aspect_crops": {"16:9": [0.0, 0.1, 1.0, 0.9], "21:9": [0.0, 0.0, 1.0, 1.0]}}
    result = _aspect_crops(item)
    assert result == {"16:9": [0.0, 0.1, 1.0, 0.9]}


def test_aspect_crops_all_invalid_is_none():
    item = {"aspect_crops": {"16:9": [2.0, 0.0, 3.0, 1.0]}}
    assert _aspect_crops(item) is None


# --- migration 0006 --------------------------------------------------------------------------

def _cfg(db_path: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "artwork.db")


def test_0006_applies_cleanly_from_0005(db_path):
    cfg = _cfg(db_path)
    command.upgrade(cfg, "0005_artwork_series_resolution")
    insp = inspect(create_engine(f"sqlite:///{db_path}"))
    cols_before = {c["name"] for c in insp.get_columns("artworks")}
    assert "aspect_crops_json" not in cols_before

    command.upgrade(cfg, "0006_artwork_aspect_crops")
    insp = inspect(create_engine(f"sqlite:///{db_path}"))
    cols_after = {c["name"] for c in insp.get_columns("artworks")}
    assert "aspect_crops_json" in cols_after


def test_0006_is_idempotent(db_path):
    """A create_all-built DB already has `aspect_crops_json` (models.py declares it). Stamping it at
    0005 and upgrading to head must not raise — the guarded add_column skips the existing column
    (defensive-migration pattern, ADR-035)."""
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    cfg = _cfg(db_path)
    command.stamp(cfg, "0005_artwork_series_resolution")
    command.upgrade(cfg, "head")  # add_column guard must skip; the column already exists
    insp = inspect(eng)
    cols = {c["name"] for c in insp.get_columns("artworks")}
    assert "aspect_crops_json" in cols
