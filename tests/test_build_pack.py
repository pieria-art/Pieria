"""build_pack pure-function units (no network). Focus: the Wikimedia native-fetch upgrade
(ADR-038 / CURATION-v2) — the catalog stores width=3840 source_urls, but the pack must fetch the
native original so masters clear the >=5120 4K floor. See [[catalog-3840-vs-pack-5120]]."""

import httpx
import pytest

from tools import build_pack
from tools.aic_tiles import image_id_of, is_aic_iiif
from tools.build_pack import _pack_fetch_url


def test_pack_fetch_url_strips_wikimedia_width():
    """A width-capped Wikimedia Special:FilePath URL is rewritten to the native original (no width)."""
    su = "https://commons.wikimedia.org/wiki/Special:FilePath/Mona%20Lisa.jpg?width=3840"
    out = _pack_fetch_url(su)
    assert "width=" not in out
    assert out.startswith("https://commons.wikimedia.org/wiki/Special:FilePath/Mona%20Lisa.jpg")


def test_pack_fetch_url_preserves_other_query_params():
    su = "https://commons.wikimedia.org/wiki/Special:FilePath/X.jpg?width=3840&page=2"
    out = _pack_fetch_url(su)
    assert "width=" not in out
    assert "page=2" in out


def test_pack_fetch_url_leaves_museum_urls_unchanged():
    """Museum full/max originals are already native-max — untouched."""
    su = "https://images.metmuseum.org/CRDImages/ep/original/DP-24049-001.jpg"
    assert _pack_fetch_url(su) == su


def test_pack_fetch_url_leaves_non_filepath_wikimedia_unchanged():
    su = "https://upload.wikimedia.org/wikipedia/commons/e/ea/Some_File.jpg"
    assert _pack_fetch_url(su) == su


def test_is_aic_iiif():
    assert is_aic_iiif("https://www.artic.edu/iiif/2/abc-123/full/max/0/default.jpg")
    assert is_aic_iiif("https://www.artic.edu/iiif/2/abc/1024,0,512,512/full/0/default.jpg")
    assert not is_aic_iiif("https://commons.wikimedia.org/wiki/Special:FilePath/X.jpg")
    assert not is_aic_iiif("https://images.metmuseum.org/CRDImages/ep/original/DP.jpg")


def test_aic_image_id_of():
    assert image_id_of("https://www.artic.edu/iiif/2/4a076002-dffe/full/max/0/default.jpg") == "4a076002-dffe"
    assert image_id_of("https://www.artic.edu/iiif/2/xyz/2048,0,1024,1024/full/0/default.jpg") == "xyz"
    assert image_id_of("https://commons.wikimedia.org/wiki/Special:FilePath/X.jpg") is None


# --- size-aware streaming fetch (ADR-040 Step-5 huge-native timeout fix) ---------------------

def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_fetch_bytes_streams_full_image():
    """A normal 200 image streams through and returns the exact bytes (no total-time ceiling)."""
    body = b"\xff\xd8\xff" + b"pixels" * 500
    async with _mock_client(lambda r: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=body)) as c:
        assert await build_pack._fetch_bytes(c, "https://example.org/master.jpg") == body


@pytest.mark.asyncio
async def test_fetch_bytes_enforces_byte_cap(monkeypatch):
    """A body past FETCH_MAX_BYTES is refused (runaway guard) rather than buffered forever."""
    monkeypatch.setattr(build_pack, "FETCH_MAX_BYTES", 1024)
    big = b"x" * 8192
    async with _mock_client(lambda r: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=big)) as c:
        assert await build_pack._fetch_bytes(c, "https://example.org/gigapixel.jpg") is None


@pytest.mark.asyncio
async def test_fetch_bytes_follows_redirect_then_streams(monkeypatch):
    """A redirect hop is followed (SSRF-validated) and the final image streams back."""
    monkeypatch.setattr(build_pack.federation, "_assert_public_url", lambda url: None)  # hermetic: skip DNS
    body = b"\xff\xd8final"

    def handler(request):
        if request.url.path == "/redir.jpg":
            return httpx.Response(302, headers={"location": "https://example.org/real.jpg"})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=body)

    async with _mock_client(handler) as c:
        assert await build_pack._fetch_bytes(c, "https://example.org/redir.jpg") == body


@pytest.mark.asyncio
async def test_fetch_bytes_rejects_non_image():
    async with _mock_client(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")) as c:
        assert await build_pack._fetch_bytes(c, "https://example.org/page") is None
