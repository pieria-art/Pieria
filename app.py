#!/usr/bin/env python3
"""
FastAPI Backend for Pieria.
Phase 4: Targeted WebSocket Routing for Multiple Displays.
"""

# NOTE — "kept-bound" imports (the dual-patch pattern). Several names in this file look unused *here*
# but MUST stay imported at module scope. Their call sites moved into core/* and routers/* during the
# app-split, yet tests still reach them via `app` (a.k.a. `app_module`): either monkeypatching
# `app_module.X` — which only works when `app` and the real call site share the *same* module object,
# so the name must remain bound here — or importing a helper/constant straight off `app`. Each such
# import is tagged with the test(s) that depend on it; don't drop one without checking them first.
import asyncio  # noqa: F401 — tests/test_download.py patches app_module.asyncio.sleep
import logging

import pillow_heif
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# Load environment variables
load_dotenv()

# Teach Pillow to read HEIC/HEIF so iPhone photos (the default capture format) decode through the
# normal Image.open() path everywhere. Upload handlers transcode to a browser-renderable format —
# browsers can't display HEIC either, so decode alone isn't enough.
pillow_heif.register_heif_opener()

# -----------------------------------------------------------------------------
# 1. Configuration, Logging & Targeted WebSocket Manager
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("artwork-display-api")

# Local imports (see the dual-patch NOTE above for why several look unused).
import httpx  # noqa: F401 — tests/test_download.py + tests/test_catalog.py patch app_module.httpx.AsyncClient

import federation  # noqa: F401 — tests/test_federation.py + tests/test_publisher_api.py patch app_module.federation.*

# ARTWORK_ROOT + STATIC_DIR are used below (static mounts); LIBRARY_DIR is kept bound because
# tests/test_display_image.py, test_factory_reset.py + test_playlist_resume.py monkeypatch it.
from config import APP_VERSION, ARTWORK_ROOT, DEMO_MODE, LIBRARY_DIR, STATIC_DIR  # noqa: F401

# Targeted WebSocket connection registry (shared by the ws + remote push paths).
from core.connections import ConnectionManager, manager  # noqa: F401

# Public demo mode (SD_DEMO_MODE=1) — default-DENY ASGI gate, no-op unless set. See core/demo.py.
from core.demo import DemoModeMiddleware  # noqa: E402

# SSRF-safe downloader (see core/downloads.py); tests/test_download.py imports it off `app`.
from core.downloads import _download_image_to_library  # noqa: F401,E402

# Boot machinery (leader election, migrations, filesystem sync, factory seed, Canvas warmer) — see
# core/lifespan.py. `lifespan` is passed to FastAPI() below.
from core.lifespan import (
    lifespan,
    sync_db_with_filesystem,  # noqa: F401 — re-exported for tests/test_playlist_resume.py
)

# Derivative-image primitives (see core/media.py); tests/test_factory_reset.py + test_display_image.py
# monkeypatch DERIVATIVES_DIR and import DISPLAY_MAX_EDGE off `app`.
from core.media import DERIVATIVES_DIR, DISPLAY_MAX_EDGE, USER_UPLOAD_MAX_BYTES  # noqa: F401,E402

# Origin/CORS trust checks used by the middleware below (see core/security.py).
from core.security import (  # noqa: E402
    _PUBLIC_FEED_GET_PREFIXES,
    _origin_allowed,
)

# Settings-table + schedule helpers (see core/settings_util.py); tests/test_schedule.py imports
# DEFAULT_SCHEDULE + resolve_schedule_state off `app`.
from core.settings_util import (  # noqa: E402
    DEFAULT_SCHEDULE,  # noqa: F401
    resolve_schedule_state,  # noqa: F401
)

# SessionLocal: tests/test_connection_manager.py monkeypatches app_module.SessionLocal (the /ws route
# reads its own binding in routers/ws.py).
from database import SessionLocal  # noqa: F401,E402

# Leaf domain routers extracted from app.py (Phase 1 + Phase 2 + Phase 3 of the app-split refactor).
# Each is a plain APIRouter with no dependency on this module — see routers/__init__.py for the
# import rule.
from routers.admin import router as admin_router
from routers.backup import router as backup_router
from routers.catalog import _read_local_json  # noqa: F401  — re-exported for tests/test_cache.py
from routers.catalog import router as catalog_router
from routers.curation import router as curation_router
from routers.demo import router as demo_router
from routers.display import router as display_router
from routers.federation import router as federation_router
from routers.health import router as health_router
from routers.library import router as library_router
from routers.packs import router as packs_router
from routers.pages import router as pages_router
from routers.publisher import router as publisher_router
from routers.settings import router as settings_router
from routers.studio import PERSONAL_PLAYLIST_NAME  # noqa: F401  — re-exported for tests/test_personal.py etc.
from routers.studio import router as studio_router
from routers.ws import router as ws_router

# Demo mode also turns off FastAPI's own introspection surface (/docs, /redoc, /openapi.json) — they're
# not app routes core/demo.py's gate can pattern-match, so the only clean "off" is never registering them.
_demo_docs_kwargs = {"docs_url": None, "redoc_url": None, "openapi_url": None} if DEMO_MODE else {}
app = FastAPI(title="Pieria", version=APP_VERSION, lifespan=lifespan, **_demo_docs_kwargs)


# --- M4: body-size cap enforced BEFORE Starlette parses/spools multipart (ADR-119 audit, 2026-09-22) --
# core/media.py's read_capped_upload runs inside the route handler, but by then Starlette has already
# parsed the multipart body (and, for large bodies, spooled it to disk) to hand FastAPI an UploadFile —
# the handler-level cap is too late to bound that work. This is a raw ASGI middleware (not
# BaseHTTPMiddleware, which itself buffers) sitting below routing: a Content-Length pre-check rejects
# oversized requests with 413 before any body is read, and a running byte count over the receive
# stream catches chunked/no-Content-Length bodies that lie about size. Scoped to the two upload routes
# only — every other endpoint is untouched. The per-handler check in core/media.py stays as
# defense-in-depth (e.g. a client whose declared boundary overhead undercounts).
_CAPPED_UPLOAD_PATHS = frozenset({"/upload", "/upload/personal"})
# Small multipart overhead allowance (boundary + per-part headers) atop the raw file cap.
_UPLOAD_BODY_CAP = USER_UPLOAD_MAX_BYTES + 64 * 1024


class UploadBodyCapMiddleware:
    """Pure ASGI middleware — rejects an oversized POST to a capped upload path with 413 before the
    body reaches Starlette's multipart parser."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or scope.get("path") not in _CAPPED_UPLOAD_PATHS):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > _UPLOAD_BODY_CAP:
                    await self._reject(send)
                    return
            except ValueError:
                pass  # malformed header — the running-total check below still applies

        # Drain + count the body ourselves so a chunked/no-Content-Length request can't lie about
        # size; buffer what we've read so it can be replayed to the real app once we know it fits.
        buffered = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            total += len(message.get("body", b"") or b"")
            if total > _UPLOAD_BODY_CAP:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break

        idx = 0

        async def replay_receive():
            nonlocal idx
            if idx < len(buffered):
                message = buffered[idx]
                idx += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(send):
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": (f'{{"detail":"File too large (max {USER_UPLOAD_MAX_BYTES // (1024 * 1024)} MB)."}}'
                      .encode()),
        })


app.add_middleware(UploadBodyCapMiddleware)

# --- Public demo mode (SD_DEMO_MODE=1) — default-DENY ASGI gate, no-op unless set (core/demo.py) -----
app.add_middleware(DemoModeMiddleware)

# Leaf domain routers (Phase 1 + Phase 2 + Phase 3 + Phase 4 of the app-split refactor — see
# .ai/refactor_app_split_plan.md).
# Order matches the (alphabetical) import block above; these are leaf domain routers with distinct
# path prefixes, so registration order carries no route-matching significance.
app.include_router(admin_router)
app.include_router(backup_router)
app.include_router(catalog_router)
app.include_router(curation_router)
app.include_router(demo_router)
app.include_router(display_router)
app.include_router(federation_router)
app.include_router(health_router)
app.include_router(library_router)
app.include_router(packs_router)
app.include_router(pages_router)
app.include_router(publisher_router)
app.include_router(settings_router)
app.include_router(studio_router)
app.include_router(ws_router)

# M6 (INFRA-D075, 2026-09-25): these two were `@app.middleware("http")` (BaseHTTPMiddleware) —
# converted to pure ASGI, matching UploadBodyCapMiddleware/DemoModeMiddleware above. Root cause of the
# thumbnail-storm incident was sessions held across Pillow work (fixed at the endpoints — see
# core/media.py's lookup_artwork_filename), but BaseHTTPMiddleware is ALSO a documented leak vector on
# its own: it runs the downstream app in a separate anyio task from the one the ASGI server cancels on
# client disconnect, so a disconnect mid-request can leave that inner task's `finally` blocks
# (including a Depends(get_db) session's own close()) never run — silently leaking a pool connection
# per disconnect regardless of what the endpoint does. Pure ASGI middleware runs in the SAME task as
# the request, so a server-side cancellation on disconnect propagates straight through the endpoint's
# own exception handling, same as having no middleware at all. Behavior (headers, CORS/origin rules)
# and add_middleware ordering relative to UploadBodyCapMiddleware/DemoModeMiddleware are unchanged.
class CacheHeadersMiddleware:
    """Pure ASGI port of the former inject_aggressive_cache_headers — identical header logic."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        is_media_cacheable = (
            (path.startswith("/artworks/") and ("thumbnail" in path or "preview" in path))
            or path.startswith("/media/")
            or path.endswith((".svg", ".png", ".jpg", ".webp"))
        )
        is_code_asset = path.endswith((".css", ".js", ".json"))
        is_html_asset = path.endswith(".html") or path in ("/admin", "/remote", "/studio", "/help", "/")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if path.startswith("/api/") or is_html_asset or path.startswith("/display/"):
                    # API/HTML and the per-display e-ink endpoint must never be cached.
                    # (/display/*.png must beat the is_media_cacheable .png rule below.)
                    headers.append((b"cache-control", b"no-store, no-cache, must-revalidate"))
                elif is_media_cacheable:
                    # Images/media rarely change — cache aggressively
                    headers.append((b"cache-control", b"public, max-age=31536000, immutable"))
                elif is_code_asset:
                    # JS/CSS/JSON change during development — short cache + revalidate
                    headers.append((b"cache-control", b"public, max-age=60, must-revalidate"))
                # Security headers (L2). nosniff is defense-in-depth against the /media
                # MIME-confusion XSS class (H1); Referrer-Policy keeps LAN paths out of any outbound
                # Referer.
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"referrer-policy", b"same-origin"))
                message = dict(message)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --- CORS + cross-origin state-change guard (ADR-036) ------------------------------------------------
# The app is a no-login LAN kiosk (ADR-013/015): the trust boundary is "you are a device on my LAN".
# The old wildcard `CORSMiddleware(allow_origins=["*"], allow_credentials=True)` silently widened that
# to "any browser tab on the LAN can drive the full API cross-origin". We replace it with a policy that
# keeps the no-login model honest:
#   * Cross-origin STATE CHANGES are blocked — a hostile page (bad ad, phishing tab) always sends an
#     Origin header on a cross-origin state-changing fetch; curl/native integrations send none and are
#     allowed (the accepted LAN-presence risk, unchanged).
#   * The read-only public FEED (what integrations consume) stays cross-origin readable.
#   * Admin/library GETs are NOT cross-origin readable (no ACAO) — a hostile tab can't exfiltrate them.
#   * Same-origin (the kiosk's own page) and explicitly configured SD_ALLOWED_ORIGINS always pass.
# _origin_allowed, _PUBLIC_FEED_GET_PREFIXES now live in core/security.py (imported above).
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class CorsAndOriginGuardMiddleware:
    """Pure ASGI port of the former cors_and_origin_guard — identical CORS/origin logic."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers_in = {k.decode("latin-1").lower(): v.decode("latin-1")
                      for k, v in scope.get("headers") or []}
        origin = headers_in.get("origin", "")
        host = headers_in.get("host", "")
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        allowed = _origin_allowed(origin, host)
        # A CORS preflight advertises the real method it is clearing; judge against that, not OPTIONS.
        effective_method = (headers_in.get("access-control-request-method", "GET").upper()
                            if method == "OPTIONS" else method)
        is_public_read = effective_method in ("GET", "HEAD") and path.startswith(_PUBLIC_FEED_GET_PREFIXES)

        # The teeth: refuse a cross-origin state change from a browser tab (blocks the preflight too).
        if effective_method in _MUTATING_METHODS and origin and not allowed:
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            })
            await send({"type": "http.response.body", "body": b"cross-origin request blocked"})
            return

        extra_headers = []
        if origin and (is_public_read or allowed):
            extra_headers.append((b"access-control-allow-origin",
                                   b"*" if is_public_read else origin.encode("latin-1")))
            extra_headers.append((b"vary", b"Origin"))
            if method == "OPTIONS":
                extra_headers.append((b"access-control-allow-methods",
                                       b"GET, POST, PUT, PATCH, DELETE, OPTIONS"))
                req_headers = headers_in.get("access-control-request-headers") or "*"
                extra_headers.append((b"access-control-allow-headers", req_headers.encode("latin-1")))
                extra_headers.append((b"access-control-max-age", b"600"))

        if method == "OPTIONS" and origin:
            await send({"type": "http.response.start", "status": 204, "headers": list(extra_headers)})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start" and extra_headers:
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + extra_headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(CacheHeadersMiddleware)
app.add_middleware(CorsAndOriginGuardMiddleware)


# POST /api/admin/factory-reset now lives in routers/admin.py.

# GET /next-image, /display/{display_id}/current.{ext}, /artworks/{artwork_id}/display.jpg,
# /api/telemetry/heartbeat, /api/displays/{display_id}/preferred-playlist, and
# /api/displays/{display_id}/schedule-state now live in routers/display.py.
#
# GET /api/remote/displays, GET /api/displays/{display_id}/now-playing, POST /api/remote/change,
# and WEBSOCKET /ws/{display_id} now live in routers/ws.py.


# -----------------------------------------------------------------------------
# 4.5 Settings (API Keys) — GET/POST /api/settings/keys* now live in routers/settings.py.
# -----------------------------------------------------------------------------


# POST /api/settings/keys/{source} now lives in routers/settings.py.

# -----------------------------------------------------------------------------
# 4.6 AI Engine (model provider configuration) — /api/settings/ai*, /api/settings/ai/oauth/* now
# live in routers/settings.py.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# 4.7 Catalog (browseable curated public-domain art; lazy high-res on add)
# -----------------------------------------------------------------------------
# CATALOG_DIR, _read_local_json, _subscribed_summaries, _subscribed_collection, _catalog_index,
# _catalog_collection, _download_and_create_artwork, and every /api/catalog* route now live in
# routers/catalog.py (imported above; _read_local_json is re-exported for tests/test_cache.py).

# ---------------------------------------------------------------------------
# §4.8 Samsung Frame TV push (Integrations)
# ---------------------------------------------------------------------------
# _frame_select (the selector shared with core/lifespan.py's frame_push_loop AND routers/settings.py's
# "Test / Push now" route) now lives in core/playback.py — see that module's
# docstring for why. The /api/settings/frame*, /api/settings/catalog* routes now live in
# routers/settings.py; the /api/catalog* browse + add routes now live in routers/catalog.py.
# Federation (/api/subscriptions*) now lives in routers/federation.py.
# Publisher Studio (/api/publisher/*) now lives in routers/publisher.py.

# -----------------------------------------------------------------------------
# 5. Static File Serving
# -----------------------------------------------------------------------------
if ARTWORK_ROOT.exists():
    app.mount("/media", StaticFiles(directory=str(ARTWORK_ROOT)), name="media")
# The /admin, /help, /studio, /remote page routes + the /publisher redirect now live in
# routers/pages.py.

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)

