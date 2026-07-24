"""
Shared robust downloader (`_download_image_to_library`) + the discovery-approve route that now
routes through it. The point of the refactor was to give the discovery path the same hardening the
catalog path already had — most importantly a descriptive User-Agent (Wikimedia/NASA reject the
default httpx UA), plus 429 retry, image validation, and collision-safe filenames. No network.
"""

import asyncio
import io

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
import core.downloads as core_downloads
import routers.curation as routers_curation
from app import _download_image_to_library, app
from database import Base, get_db
from models import ArtworkModel, DiscoveryQueueModel


def _png_bytes(size=(40, 30), color=(10, 20, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content


def _fake_client_factory(responses, captured):
    """A fake httpx.AsyncClient class: yields `responses` in order across every instance it spawns,
    and records the headers it was constructed with + the URLs it was asked to GET."""
    seq = list(responses)

    class _Client:
        def __init__(self, *a, headers=None, **k):
            captured["headers"] = headers or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            captured.setdefault("urls", []).append(url)
            return seq.pop(0)

    return _Client


@pytest.fixture
def lib(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(core_downloads, "LIBRARY_DIR", tmp_path)
    return tmp_path


# --- core downloader -------------------------------------------------------

def test_sends_descriptive_user_agent(lib, monkeypatch):
    captured = {}
    monkeypatch.setattr(app_module.httpx, "AsyncClient",
                        _fake_client_factory([_Resp(200, _png_bytes())], captured))
    dest, name, w, h = asyncio.run(_download_image_to_library("https://x.test/a.jpg", filename="t"))
    # The whole reason the refactor exists: a real UA, not httpx's default (which Wikimedia 403s).
    assert "Pieria" in captured["headers"].get("User-Agent", "")
    assert (w, h) == (40, 30) and dest.exists() and name == "t.jpg"


def test_retries_on_429_then_succeeds(lib, monkeypatch):
    captured = {}
    monkeypatch.setattr(app_module.httpx, "AsyncClient",
                        _fake_client_factory([_Resp(429), _Resp(200, _png_bytes())], captured))
    async def _no_wait(*a, **k):
        return None
    monkeypatch.setattr(app_module.asyncio, "sleep", _no_wait)  # don't wait out the backoff
    dest, name, w, h = asyncio.run(_download_image_to_library("https://x.test/a.jpg", filename="t"))
    assert len(captured["urls"]) == 2  # retried once past the 429
    assert (w, h) == (40, 30) and dest.exists()


def test_invalid_image_raises_and_cleans_up(lib, monkeypatch):
    captured = {}
    monkeypatch.setattr(app_module.httpx, "AsyncClient",
                        _fake_client_factory([_Resp(200, b"<html>not an image</html>")], captured))
    with pytest.raises(HTTPException):
        asyncio.run(_download_image_to_library("https://x.test/a.jpg", filename="bad"))
    assert list(lib.iterdir()) == []  # the bad bytes were deleted, never left in the library


def test_non_200_raises(lib, monkeypatch):
    captured = {}
    monkeypatch.setattr(app_module.httpx, "AsyncClient",
                        _fake_client_factory([_Resp(404)], captured))
    with pytest.raises(HTTPException):
        asyncio.run(_download_image_to_library("https://x.test/missing.jpg", filename="x"))


def test_collision_safe_filename(lib, monkeypatch):
    captured = {}
    monkeypatch.setattr(app_module.httpx, "AsyncClient",
                        _fake_client_factory([_Resp(200, _png_bytes()), _Resp(200, _png_bytes())], captured))
    _, n1, *_ = asyncio.run(_download_image_to_library("https://x.test/a.jpg", filename="dup"))
    _, n2, *_ = asyncio.run(_download_image_to_library("https://x.test/a.jpg", filename="dup"))
    assert n1 == "dup.jpg" and n2 == "dup_1.jpg"
    assert (lib / "dup.jpg").exists() and (lib / "dup_1.jpg").exists()


# --- discovery-approve route now uses the core -----------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    monkeypatch.setattr(app_module, "LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(core_downloads, "LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(app_module, "ARTWORK_ROOT", tmp_path)

    # Enrichment runs as a background task; stub it so the test touches no AI/network.
    async def _noop(*a, **k):
        return None
    # /api/discover/approve now lives in routers/curation.py — it reads its own run_rag_pipeline
    # binding, so patch there (single-router helper; no app_module copy exists anymore).
    monkeypatch.setattr(routers_curation, "run_rag_pipeline", _noop)

    with TestClient(app) as c:
        yield c, db, monkeypatch
    app.dependency_overrides.clear()
    db.close()


def test_approve_discovery_uses_robust_downloader(client):
    c, db, monkeypatch = client
    captured = {}
    monkeypatch.setattr(app_module.httpx, "AsyncClient",
                        _fake_client_factory([_Resp(200, _png_bytes())], captured))
    item = DiscoveryQueueModel(
        source_url="https://commons.wikimedia.test/x.jpg", thumbnail_url="https://t/x.jpg",
        proposed_title="Test Piece", proposed_artist="Tester", source_api="wikimedia", status="pending")
    db.add(item); db.commit(); db.refresh(item)

    r = c.post(f"/api/discover/approve/{item.id}")
    assert r.status_code == 200, r.text
    # The bug this refactor fixes: the old path used a bare httpx client (default UA → Wikimedia 403).
    assert "Pieria" in captured["headers"].get("User-Agent", "")

    art = db.query(ArtworkModel).filter(ArtworkModel.title == "Test Piece").first()
    assert art is not None and art.status == "processing"
    assert art.source_url == "https://commons.wikimedia.test/x.jpg"  # now persisted for dedup
    assert art.filename.startswith(f"scouted_{item.id}_")  # orphan-cleanup still parses the id
    assert art.original_width == 40 and art.original_height == 30
