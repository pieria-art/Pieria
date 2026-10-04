"""Rijksmuseum scout fetches per-item URLs taken from an API response: they must go through the SSRF
guard (every hop), not a bare follow_redirects client."""
import httpx
import pytest

import federation
import scout
from core import safe_http


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    async def _nosleep(*a, **k):
        return None
    monkeypatch.setattr(scout.asyncio, "sleep", _nosleep)

    def guard(url):
        if "10.0.0.1" in url or "127.0.0.1" in url:
            raise federation.FederationError("blocked: non-public")
    monkeypatch.setattr(federation, "_assert_public_url", guard)


def _patch_client(monkeypatch, handler, seen):
    def handler2(req):
        seen.append(str(req.url))
        return handler(req)
    monkeypatch.setattr(safe_http, "safe_async_client",
                        lambda **k: httpx.AsyncClient(transport=httpx.MockTransport(handler2)))


@pytest.mark.asyncio
async def test_item_url_redirect_to_private_is_refused(monkeypatch):
    seen = []

    def handler(req):
        if "search/collection" in req.url.path:
            return httpx.Response(200, json={"orderedItems": [{"id": "https://id.rijksmuseum.nl/1"}]})
        return httpx.Response(302, headers={"location": "http://10.0.0.1/secret"})
    _patch_client(monkeypatch, handler, seen)
    assert await scout.RijksmuseumScout().find_art("x") == []
    assert not any("10.0.0.1" in u for u in seen)   # never requested


@pytest.mark.asyncio
async def test_item_url_pointing_at_loopback_is_refused(monkeypatch):
    seen = []

    def handler(req):
        if "search/collection" in req.url.path:
            return httpx.Response(200, json={"orderedItems": [{"id": "http://127.0.0.1:8000/admin"}]})
        return httpx.Response(200, json={})
    _patch_client(monkeypatch, handler, seen)
    assert await scout.RijksmuseumScout().find_art("x") == []
    assert not any("127.0.0.1" in u for u in seen)
