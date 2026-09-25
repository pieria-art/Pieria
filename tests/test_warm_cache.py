"""M6 (INFRA-D075 #5, 2026-09-25): the boot warm-sweep (core.lifespan.warm_all_canvas_cache) now
pre-renders BOTH the Canvas display derivative AND the admin-grid thumbnail for every approved
artwork, so a restart doesn't leave either grid re-decoding 20-60 MP masters on first request."""

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.media as core_media
from core import lifespan as L
from database import Base
from models import ArtworkModel


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.mark.asyncio
async def test_warm_all_canvas_cache_prerenders_thumbnails_too(tmp_path, monkeypatch):
    db = _db()
    library = tmp_path / "_Library"
    library.mkdir()
    Image.new("RGB", (3000, 2000), (30, 60, 90)).save(library / "a.jpg", format="JPEG", quality=90)
    art = ArtworkModel(filename="a.jpg", status="approved")
    db.add(art); db.commit(); db.refresh(art)

    monkeypatch.setattr(L, "SessionLocal", lambda: db)
    monkeypatch.setattr(L, "LIBRARY_DIR", library)
    derivatives = tmp_path / "_derivatives"
    monkeypatch.setattr(core_media, "DERIVATIVES_DIR", derivatives)

    await L.warm_all_canvas_cache()

    # The Canvas display derivative landed (pre-existing behavior).
    display_files = list(derivatives.glob(f"{art.id}-*.jpg"))
    assert display_files, "warm sweep did not pre-render the Canvas display derivative"

    # The M4 thumbnail disk-cache entry also landed — confirmed by asserting a fresh
    # get_optimized_image call for the same key never touches Pillow again.
    thumb_files = list(derivatives.glob("opt-a.jpg-400x400-*"))
    assert thumb_files, "warm sweep did not pre-render the thumbnail derivative"

    core_media._optimized_image_lru.clear()
    monkeypatch.setattr(Image, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-decoded")))
    core_media.get_optimized_image(library / "a.jpg", (400, 400), quality=70)  # must hit disk, not Pillow


@pytest.mark.asyncio
async def test_warm_all_canvas_cache_survives_one_bad_artwork(tmp_path, monkeypatch):
    """A missing/corrupt file for one artwork must not stop the sweep from warming the rest — both
    the display AND thumbnail passes are individually best-effort."""
    db = _db()
    library = tmp_path / "_Library"
    library.mkdir()
    Image.new("RGB", (200, 200), (1, 2, 3)).save(library / "good.jpg", format="JPEG")
    db.add(ArtworkModel(filename="missing.jpg", status="approved"))
    good = ArtworkModel(filename="good.jpg", status="approved")
    db.add(good); db.commit(); db.refresh(good)

    monkeypatch.setattr(L, "SessionLocal", lambda: db)
    monkeypatch.setattr(L, "LIBRARY_DIR", library)
    derivatives = tmp_path / "_derivatives"
    monkeypatch.setattr(core_media, "DERIVATIVES_DIR", derivatives)

    await L.warm_all_canvas_cache()   # must not raise

    assert list(derivatives.glob(f"{good.id}-*.jpg"))
    assert list(derivatives.glob("opt-good.jpg-400x400-*"))
