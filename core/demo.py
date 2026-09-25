"""Public demo mode (SD_DEMO_MODE=1, demo.pieria.org) — ADR pending.

The app has no auth by design (ADR-013/015): the trust boundary is normally "you are a device on my
LAN". A demo box sits behind a Cloudflare Tunnel on the open internet instead, where every request
arrives from cloudflared on localhost/the docker gateway — there is no trustworthy client IP and no
loopback exemption can exist. So demo mode replaces the LAN trust boundary with a default-DENY ASGI
gate: an explicit (method, path) allowlist, checked before routing; anything else is refused. This is
the ONLY thing standing between the public internet and every mutating route, so treat this module like
the CORS/origin guard in app.py — narrow, and grown by ADDING an allowlist entry, never by relaxing the
default.

Everything here is a no-op when `config.DEMO_MODE` is False; `config` is read fresh on every call (not
imported by value) so tests can flip it per-test without an app restart.
"""

import json
import logging
import re

logger = logging.getLogger("artwork-display-api")

# --- The allowlist ------------------------------------------------------------------------------------
# Each entry is (methods, compiled path regex). `_ID` is one non-slash path segment (an artwork id,
# display id, collection id, ...) — every dynamic route in the allowlist takes exactly one.
_ID = r"[^/]+"


def _p(*parts: str) -> re.Pattern:
    return re.compile("^" + "".join(parts) + "$")


_GET = frozenset({"GET", "HEAD"})

ALLOWED_ROUTES: tuple[tuple[frozenset, re.Pattern], ...] = (
    # Pages (static mounts are handled separately by _is_static_asset — they're not FastAPI routes).
    (_GET, _p(r"/admin")),
    (_GET, _p(r"/help")),
    (_GET, _p(r"/art/", _ID)),
    # Display feed.
    (_GET, _p(r"/next-image")),
    (_GET, _p(r"/artworks/", _ID, r"/display\.jpg")),
    (_GET, _p(r"/api/displays/", _ID, r"/preferred-playlist")),
    (_GET, _p(r"/api/displays/", _ID, r"/schedule-state")),
    (_GET, _p(r"/api/displays/", _ID, r"/now-playing")),
    # Browse.
    (_GET, _p(r"/playlists")),
    (_GET, _p(r"/artworks")),
    (_GET, _p(r"/artworks/", _ID, r"/thumbnail")),
    (_GET, _p(r"/artworks/", _ID, r"/preview")),
    (_GET, _p(r"/artworks/", _ID, r"/placard")),
    (_GET, _p(r"/api/catalog")),
    (_GET, _p(r"/api/catalog/", _ID)),
    (_GET, _p(r"/api/packs")),
    (_GET, _p(r"/api/packs/status")),
    (_GET, _p(r"/api/remote/displays")),
    (_GET, _p(r"/api/demo")),
    # Heartbeat — allowed in, but routers/display.py makes it a no-op 204 when DEMO_MODE is on.
    (frozenset({"POST"}), _p(r"/api/telemetry/heartbeat")),
)

# Paths that would otherwise match a pattern above but are explicitly excluded (spec §1): live museum
# calls (would get the demo box rate-limited by the museums themselves), checked BEFORE the allowlist
# so e.g. "/api/catalog/search" never falls through to the "/api/catalog/{collection_id}" pattern.
_DENY_OVERRIDE = frozenset({"/api/catalog/search", "/api/catalog/suggest"})

_DEMO_DENIED_BODY = json.dumps({
    "detail": "Disabled in the Pieria demo — install your own: https://github.com/pieria-art/Pieria"
}).encode()


def _is_allowed_http(method: str, path: str) -> bool:
    if path in _DENY_OVERRIDE:
        return False
    return any(method in methods and pattern.match(path) for methods, pattern in ALLOWED_ROUTES)


def normalize_display_id(display_id: str) -> str:
    """Collapse every display id to one shared 'demo' row/shuffle-bag in demo mode, so an anonymous
    visitor can't mint unbounded active_displays rows or per-display playback state. No-op otherwise.
    One helper, used everywhere a display_id reaches next-image/WS/preferred-playlist/schedule-state/
    now-playing (routers/display.py, routers/ws.py)."""
    import config
    return "demo" if config.DEMO_MODE else display_id


# --- Static asset allowance ---------------------------------------------------------------------------
# The "/" StaticFiles mount (app.py) serves the JS/CSS/images every allowed page needs (app.js,
# app.css, admin.js, /vendor/*, /docs/*.png, /logo.svg, ...). It isn't a FastAPI route (app.routes sees
# a single Mount), so it can't be enumerated in ALLOWED_ROUTES above. Instead of hardcoding the file
# list (which rots), read STATIC_DIR's own top-level entries once at import — self-maintaining as
# static/ grows. Top-level *.html files are excluded: they're the SPA shells for the denied pages
# (studio.html, remote.html, ...) and nothing legitimate ever requests them directly (the app always
# asks for the clean page routes, e.g. /studio, which the gate denies above).
def _static_top_level() -> tuple[frozenset, frozenset]:
    from config import STATIC_DIR
    files, dirs = set(), set()
    if STATIC_DIR.exists():
        for entry in STATIC_DIR.iterdir():
            if entry.is_dir():
                dirs.add(entry.name)
            elif not entry.name.endswith(".html"):
                files.add(entry.name)
    return frozenset(files), frozenset(dirs)


_STATIC_FILES, _STATIC_DIRS = _static_top_level()


def _is_static_asset(path: str) -> bool:
    if path == "/":
        return True
    name = path[1:].split("/", 1)[0]
    if "/" not in path[1:]:
        return name in _STATIC_FILES

    if name not in _STATIC_DIRS:
        return False
    # The top-level-name check alone isn't enough: a REAL app route can share a path prefix with an
    # allowed static directory without being one (e.g. FastAPI's own GET /docs/oauth2-redirect sits
    # under the allowed "docs" images directory but is a live route, not a file on disk) — found by a
    # reviewer via the sweep test. Require the target to actually be a file under STATIC_DIR: resolves
    # symlinks too, so this also backstops _has_dotdot_segment against anything sneakier than a literal
    # '..' (a symlink pointing outside STATIC_DIR, say) rather than trusting containment alone.
    from config import STATIC_DIR
    try:
        candidate = (STATIC_DIR / path.lstrip("/")).resolve()
        candidate.relative_to(STATIC_DIR.resolve())
    except (ValueError, OSError):
        return False
    return candidate.is_file()


def _has_dotdot_segment(path: str) -> bool:
    """True if any '/'-separated segment is '.' or '..'. `_is_static_asset` above only looks at the
    FIRST path segment (e.g. "catalog" in "/catalog/whatever") and trusts the rest — so without this
    check, "/catalog/../studio.html" would pass (allowed top dir "catalog") while actually resolving,
    once StaticFiles serves it, to the denied static/studio.html. Reject outright before any allow
    check runs; no legitimate request from this app ever contains a dot segment."""
    return any(seg in (".", "..") for seg in path.split("/"))


# --- Content bootstrap (SD_DEMO_PACKS) ------------------------------------------------------------
async def install_demo_packs(pack_ids: list[str], default_playlist: str = "") -> None:
    """Leader-boot task (called from core/lifespan.py): install every SD_DEMO_PACKS collection that
    isn't already an installed subscription, via the same on-demand registry installer the Art Packs
    admin card uses — idempotent, so a re-run only fetches what's missing. Best-effort per pack; one
    bad/unreachable collection must never block the others. If `default_playlist` names an installed
    (or now-installed) playlist, it's set as the `default_playlist` setting so `/` plays it."""
    from config import PACK_REGISTRY_URL
    from core import pack_fetch
    from database import SessionLocal
    from models import SettingsModel, SubscriptionModel

    db = SessionLocal()
    client = pack_fetch.new_client()
    try:
        for cid in pack_ids:
            cid = cid.strip()
            if not cid:
                continue
            if db.query(SubscriptionModel).filter(SubscriptionModel.url == f"pack:{cid}").first():
                continue  # already installed
            logger.info(f"[Demo] installing pack {cid!r}...")
            try:
                res = await pack_fetch.install_collection_from_registry(db, client, PACK_REGISTRY_URL, cid)
                if not res.get("ok"):
                    logger.warning(f"[Demo] pack {cid!r} failed: {res.get('error')}")
            except Exception as e:  # noqa: BLE001 — one bad pack must never abort the rest
                logger.warning(f"[Demo] pack {cid!r} errored: {type(e).__name__}: {e}")

        if default_playlist:
            from core.settings_util import _upsert_setting
            row = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
            if row is None or row.setting_value != default_playlist:
                _upsert_setting(db, "default_playlist", default_playlist)
                db.commit()
    finally:
        await client.aclose()
        db.close()


class DemoModeMiddleware:
    """Pure ASGI middleware — default-DENY gate for demo mode, sitting below routing (see app.py's
    UploadBodyCapMiddleware for the same raw-ASGI pattern). Checked fresh per request; a complete
    no-op when config.DEMO_MODE is False."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            # Always passes through, demo or not — this is app startup/shutdown, not a client request.
            await self.app(scope, receive, send)
            return

        import config
        if not config.DEMO_MODE:
            await self.app(scope, receive, send)
            return

        if scope_type == "websocket":
            # No WebSocket surface in demo mode, full stop — not even /ws/{id}. A public visitor's
            # socket cost a SQLite commit per {"action":"heartbeat"} frame plus its own 1 Hz
            # command-poller task server-side; too cheap to grief at scale. The display doesn't need
            # it either — static/app.js's rotation runs off /next-image on a client-side timer, never
            # the socket (only remote-control push + liveness go over WS). Close before accept.
            await send({"type": "websocket.close", "code": 1008})
            return

        if scope_type != "http":
            # Unknown/unexpected ASGI scope type — refuse to bridge it through rather than assume it's
            # safe just because it's neither http nor websocket.
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "")
        if _has_dotdot_segment(path):
            await self._deny(send)
            return
        if _is_allowed_http(method, path) or (method in _GET and _is_static_asset(path)):
            await self.app(scope, receive, send)
            return

        await self._deny(send)

    @staticmethod
    async def _deny(send):
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": _DEMO_DENIED_BODY})
