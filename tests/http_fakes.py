"""Shared helper for tests that fake `httpx.AsyncClient` with a `.get()`-only class: the downloader now
fetches through core.downloads.guarded_stream (the one SSRF hop follower), which calls `.stream()`."""
import httpx


class _Stream:
    def __init__(self, client, url, kw):
        self._client, self._url, self._kw = client, url, kw

    async def __aenter__(self):
        resp = await self._client.get(self._url, **self._kw)
        if not hasattr(resp, "headers"):
            resp.headers = {}
        if not hasattr(resp, "url"):
            resp.url = httpx.URL(self._url)
        if not hasattr(resp, "aread"):
            async def _aread():
                return resp.content
            resp.aread = _aread
        return resp

    async def __aexit__(self, *a):
        return False


def stream_via_get(self, method, url, **kw):
    kw.pop("follow_redirects", None)
    return _Stream(self, url, kw)
