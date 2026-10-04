"""M2/L1/N5: pinned-resolution SSRF-safe HTTP. No real network (conftest blocks non-loopback): the TCP
dial and the TLS handshake are faked at the httpcore seam, so what is asserted is WHICH address is
dialled, HOW MANY resolutions happen, and WHAT hostname TLS is told to verify."""
import socket

import httpcore
import httpx
import pytest
from httpcore._backends.anyio import AnyIOBackend

import federation
from core import safe_http
from core.downloads import guarded_stream


def _gai_table(table, calls=None):
    """getaddrinfo fake: `table[host]` is a list of answers; a list-of-lists means 'answer N differs per call'
    (the rebinding attacker). Records every lookup in `calls`."""
    seen = {}

    def gai(host, port, *a, **k):
        if calls is not None:
            calls.append(host)
        ans = table[host]
        if ans and isinstance(ans[0], list):
            n = seen.get(host, 0)
            seen[host] = n + 1
            ans = ans[min(n, len(ans) - 1)]
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, 1, 6, "", (ip, port)) for ip in ans]
    return gai


class _Dial:
    """Replaces AnyIOBackend.connect_tcp: records the dialled host and returns a stream whose TLS
    handshake records the server_hostname it was asked to verify, then aborts."""
    def __init__(self):
        self.dialled, self.tls_hostnames = [], []

    async def __call__(self, backend, host, port, **kw):
        self.dialled.append(host)
        outer = self

        class _Stream(httpcore.AsyncNetworkStream):
            async def read(self, max_bytes, timeout=None): raise httpcore.ReadError("fake")
            async def write(self, buffer, timeout=None): raise httpcore.WriteError("fake")
            async def aclose(self): pass
            async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                outer.tls_hostnames.append(server_hostname)
                raise httpcore.ConnectError("fake tls abort")
            def get_extra_info(self, info): return None
        return _Stream()


@pytest.fixture
def dial(monkeypatch):
    d = _Dial()
    monkeypatch.setattr(AnyIOBackend, "connect_tcp", lambda self, host, port, **kw: d(self, host, port, **kw))
    return d


async def _get(url):
    async with safe_http.safe_async_client() as c:
        return await c.get(url)


@pytest.mark.asyncio
async def test_rebinding_public_then_private_dials_first_validated_ip_once(monkeypatch, dial):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo",
                        _gai_table({"evil.test": [["93.184.216.34"], ["127.0.0.1"]]}, calls))
    with pytest.raises(httpx.ConnectError):    # our fake TLS aborts; irrelevant — we inspect the dial
        await _get("https://evil.test/x")
    assert dial.dialled == ["93.184.216.34"]   # the validated literal, never the host name
    assert calls == ["evil.test"]              # ONE resolution total: nothing for rebinding to flip


@pytest.mark.asyncio
async def test_tls_still_verifies_the_original_hostname(monkeypatch, dial):
    monkeypatch.setattr(socket, "getaddrinfo", _gai_table({"cdn.example.test": ["93.184.216.34"]}))
    with pytest.raises(httpx.ConnectError):
        await _get("https://cdn.example.test/x")
    assert dial.dialled == ["93.184.216.34"]
    assert dial.tls_hostnames == ["cdn.example.test"]   # SNI + cert check use the name, not the IP


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "192.168.0.9", "172.16.5.5", "169.254.169.254", "0.0.0.0",
    "100.64.0.1", "198.18.0.1",                       # L1: CGNAT, benchmarking
    "::1", "fe80::1", "fc00::1", "fd12:3456::1",       # loopback, link-local, ULA
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",   # v4-mapped
    "224.0.0.1",
])
async def test_non_public_answers_are_refused_before_any_dial(monkeypatch, dial, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _gai_table({"bad.test": [ip]}))
    with pytest.raises(httpx.ConnectError, match="non-public"):
        await _get("http://bad.test/")
    assert dial.dialled == []


@pytest.mark.asyncio
async def test_one_private_record_among_public_ones_refuses_the_host(monkeypatch, dial):
    monkeypatch.setattr(socket, "getaddrinfo", _gai_table({"mix.test": ["93.184.216.34", "10.0.0.1"]}))
    with pytest.raises(httpx.ConnectError):
        await _get("http://mix.test/")
    assert dial.dialled == []


@pytest.mark.asyncio
async def test_ip_literal_urls_are_validated_too(monkeypatch, dial):
    with pytest.raises(httpx.ConnectError, match="non-public"):
        await _get("http://127.0.0.1:8000/")
    with pytest.raises(httpx.ConnectError, match="non-public"):
        await _get("http://[::ffff:10.0.0.1]/")
    assert dial.dialled == []


def test_client_defaults_never_follow_redirects_or_trust_env_proxies():
    c = safe_http.safe_async_client()
    assert c.follow_redirects is False and c._trust_env is False


def test_pool_really_uses_the_pinned_backend():
    t = safe_http.safe_transport()
    assert isinstance(t._pool._network_backend, safe_http.PinnedBackend)


# --- N5: every redirect hop is validated by the ONE follower -----------------

def _redirecting_client(hops):
    def handler(req):
        loc = hops.get(str(req.url))
        if loc:
            return httpx.Response(302, headers={"location": loc})
        return httpx.Response(200, content=b"ok")
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [
    "http://rebind.test/p", "http://127.0.0.1:8000/admin", "http://[::1]/", "http://100.64.0.5/",
])
async def test_redirect_to_non_public_refused_at_every_hop(monkeypatch, target):
    monkeypatch.setattr(socket, "getaddrinfo", _gai_table({
        "a.test": ["93.184.216.34"], "b.test": ["93.184.216.35"], "rebind.test": ["10.0.0.1"],
        "127.0.0.1": ["127.0.0.1"], "::1": ["::1"], "100.64.0.5": ["100.64.0.5"]}))
    # Bad address as hop 2, and as hop 3 after a clean hop 2.
    for chain in ({"http://a.test/": target},
                  {"http://a.test/": "http://b.test/", "http://b.test/": target}):
        async with _redirecting_client(chain) as client:
            with pytest.raises(federation.FederationError):
                async with guarded_stream(client, "GET", "http://a.test/") as r:
                    pytest.fail(f"followed a redirect to {target}: {r.status_code}")


@pytest.mark.asyncio
async def test_public_redirect_chain_is_followed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai_table({
        "a.test": ["93.184.216.34"], "b.test": ["93.184.216.35"]}))
    async with _redirecting_client({"http://a.test/": "http://b.test/final"}) as client:
        async with guarded_stream(client, "GET", "http://a.test/") as r:
            assert r.status_code == 200 and str(r.url) == "http://b.test/final"
