"""SSRF-safe outbound HTTP (M2/L1/N5): resolve ONCE, validate, connect to the validated IP.

The old guard (`federation._assert_public_url`) resolved the host and checked the answers, then httpx
re-resolved at connect time — a DNS-rebinding server could answer public, then private. Here the
check and the connect are the SAME resolution: a custom httpcore network backend resolves the host
inside `connect_tcp`, refuses the connection if ANY answer is non-public, and opens the socket to a
validated IP literal. httpcore still performs TLS with the ORIGIN hostname (SNI + certificate
verification) and httpx still sends the original Host header — only the TCP dial is pinned.

`safe_async_client()` is the one client factory every outbound fetch of a user/third-party URL uses.
Redirects stay off on it; follow hops with `core.downloads.guarded_stream` (the one hop follower).
"""

import asyncio
import ipaddress
import socket

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend

_SITE_LOCAL = ipaddress.ip_network("fec0::/10")


class UnsafeAddress(Exception):
    """A host resolved to (or is) a non-public address."""


def check_ip(ip_text: str) -> None:
    """L1: allowlist on `is_global` (excludes private, loopback, link-local, ULA, CGNAT 100.64/10,
    198.18/15, v4-mapped private, ...) plus explicit multicast/reserved/unspecified refusals (a few
    such ranges report is_global=True)."""
    ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _SITE_LOCAL:   # fec0::/10, deprecated site-local: is_global still says True
        raise UnsafeAddress(f"host resolves to a non-public address ({ip}) — blocked")
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise UnsafeAddress(f"host resolves to a non-public address ({ip}) — blocked")


def resolve_public(host: str, port: int) -> list[str]:
    """Blocking: resolve `host` and return its addresses, raising UnsafeAddress if ANY is non-public
    (or none resolve). Callers off the event loop only (asyncio.to_thread)."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeAddress(f"cannot resolve host: {e}") from e
    ips: list[str] = []
    for info in infos:
        addr = info[4][0]
        check_ip(addr)
        if addr not in ips:
            ips.append(addr)
    if not ips:
        raise UnsafeAddress("cannot resolve host: no addresses")
    return ips


class _BlockedConnect(httpcore.ConnectError):
    """Raised from the backend so httpx surfaces it as httpx.ConnectError (an httpx.HTTPError)."""


class PinnedBackend(AnyIOBackend):
    """httpcore backend: single resolution + validation + connect to the validated IP literal."""

    def __init__(self, resolver=resolve_public):
        super().__init__()
        self._resolve = resolver

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            ips = await asyncio.to_thread(self._resolve, host, port)
        except UnsafeAddress as e:
            raise _BlockedConnect(str(e)) from e
        last: Exception | None = None
        for ip in ips:   # only addresses from the ONE validated resolution; never re-resolved
            try:
                return await super().connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                                 socket_options=socket_options)
            except httpcore.ConnectError as e:
                last = e
        raise last or _BlockedConnect("no connectable address")


def safe_transport(*, backend: PinnedBackend | None = None, **kw) -> httpx.AsyncHTTPTransport:
    """An AsyncHTTPTransport whose connection pool dials through PinnedBackend (verify/HTTP2 etc.
    are the httpx defaults; TLS verifies against the origin hostname)."""
    t = httpx.AsyncHTTPTransport(**kw)
    # httpx exposes no hook for the network backend; swap the pool's (a covered-by-test private attr —
    # tests/test_safe_http.py fails loudly if an httpcore bump renames it, rather than silently unpinning).
    if not hasattr(t._pool, "_network_backend"):
        raise RuntimeError("httpcore pool layout changed — cannot pin the network backend")
    t._pool._network_backend = backend or PinnedBackend()
    return t


def safe_async_client(**kwargs) -> httpx.AsyncClient:
    """`httpx.AsyncClient` with a pinned-resolution transport. Env proxies are ignored (a proxy would
    be the validated hop, not the target). Redirects default OFF."""
    kwargs.setdefault("follow_redirects", False)
    kwargs.setdefault("trust_env", False)
    kwargs.setdefault("transport", safe_transport())
    return httpx.AsyncClient(**kwargs)
