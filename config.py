"""
Shared configuration constants for the Pieria application.
Extracted from app.py to break circular import dependencies.
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Version + update channel (ADR-071) --------------------------------------------------------------
# Single source of truth for the running version. Bump this WHEN YOU CUT A RELEASE and tag the commit
# `vX.Y.Z` — the update check compares this against the latest GitHub Release, and sd-update checks out
# that tag. Keep the two in lockstep: the tag is what ships, this is what the box believes it is running.
APP_VERSION = "1.0.0"

# owner/repo whose GitHub Releases define "latest". Public info; overridable for a fork.
UPDATE_REPO = os.getenv("SD_UPDATE_REPO", "pieria-art/Pieria").strip()

# C1: AI enrichment sometimes emits Markdown emphasis (e.g. "*The Irish Question*"). The placard and
# /art page render plain text, so the markers show literally. Flatten inline emphasis to plain prose.
# Lives here (dep-free) so app.py, curator.py and agents.py all share one implementation; mirrored by
# stripMd() in static/app.js for the client-rendered Canvas placard.
_MD_STRIP = [
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"), (re.compile(r"\*([^*]+)\*"), r"\1"),
    (re.compile(r"__([^_]+)__"), r"\1"), (re.compile(r"_([^_]+)_"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"), (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),
]


def strip_markdown(s: str | None) -> str:
    s = s or ""
    for pat, repl in _MD_STRIP:
        s = pat.sub(repl, s)
    return s

ARTWORK_ROOT = Path(os.getenv("ARTWORK_ROOT", "Artwork"))
LIBRARY_DIR = ARTWORK_ROOT / "_Library"

# Where the static frontend (admin/help/studio/remote pages + the JS/CSS bundle) is served from.
# Lives here (not app.py) so both app.py's static mount and routers/pages.py's page routes share
# one constant instead of two independently-defined `Path("static")` literals.
STATIC_DIR = Path("static")

# Namespace prefix for federated-subscription pseudo-collection ids in the catalog browse surface
# (e.g. "sub_3") — keeps them from ever colliding with, or masquerading as, a bundled/official
# collection id. Shared by routers/federation.py (mints `sub_<id>`) and app.py's catalog-merge
# internals (which consume it to detect/resolve a subscription id).
SUB_PREFIX = "sub_"

# Wikimedia (and most museum/image hosts) reject the default httpx User-Agent; every outbound image
# fetch must send this descriptive UA. Lives here (not app.py) so the offline tools/ scripts can
# reuse it without importing the FastAPI app.
SD_USER_AGENT = "Pieria/1.0 (https://github.com/pieria-art/Pieria; art display) httpx"

# Modular-pack registry (ADR-040 #4 / ADR-038 §5): the public packs.json the "browse & download packs"
# card reads to offer on-demand collections. Default = the official Cloudflare R2 host behind curwe.ai;
# overridable per-install via the `pack_registry_url` setting (e.g. to point at a staging registry).
PACK_REGISTRY_URL = os.getenv("SD_PACK_REGISTRY_URL", "https://packs.curwe.ai/packs.json")

# Test/CI escape hatch (F1 hang, 2026-08-29): the OOB first-boot seed (core/lifespan.seed_from_registry)
# reaches the real network at app startup, which a GH runner can't (403s forever) — starting a retry
# loop that used to survive shutdown. Set by tests/conftest.py before `app` is imported so no test
# TestClient ever spawns it; production never sets this. Tests that exercise the seed on purpose
# (tests/test_oob_seed.py) monkeypatch it back off.
DISABLE_BOOT_SEED = os.getenv("SD_DISABLE_BOOT_SEED", "").strip() == "1"

# Deployment mode. Only the all-in-one appliance compose override sets SD_APPLIANCE_MODE=all-in-one;
# the generic/MS-01 server and thin-client (display-only) topologies leave it unset. Gates the
# host-health console + the GUI update bridge — surfaces that only make sense when the server runs
# ON the device being managed. A thin client's admin is served by a remote box that lacks this flag,
# so those surfaces correctly never appear there.
APPLIANCE_MODE = os.getenv("SD_APPLIANCE_MODE", "").strip().lower()  # "" | "all-in-one"
IS_APPLIANCE = APPLIANCE_MODE == "all-in-one"

# Where the appliance bridge exchanges files with the host helper. ./data is already bind-mounted to
# /app/data, so the unprivileged container can write here and a root systemd watcher can read it.
APPLIANCE_DIR = Path(os.getenv("APPLIANCE_DIR", "data/appliance"))

# --- Security posture (ADR-036: no-login LAN kiosk kept honest by scoped CORS + gated mutations) -----
# The app has no auth by design (ADR-013/015) — the trust boundary is "you are a device on my LAN".
# Wildcard CORS previously widened that to "any browser tab on the LAN", so state-changing requests now
# carry an Origin allowlist check (see app.py). Same-origin (the kiosk's own page) is always allowed;
# add extra LAN origins here only to drive the API cross-origin from another device.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("SD_ALLOWED_ORIGINS", "").split(",") if o.strip()]

# --- Public demo mode (demo.pieria.org) -----------------------------------------------------------
# SD_DEMO_MODE=1 puts the app behind a default-DENY ASGI gate (core/demo.py) so an anonymous internet
# visitor gets a read-only browse of pre-baked art — no auth exists (ADR-013/015), so this is the ONLY
# thing standing between the public internet and every mutating route. Off (default) -> zero behavior
# change; every other route in this file is untouched by demo mode.
DEMO_MODE = os.getenv("SD_DEMO_MODE", "").strip() == "1"

# Comma-separated pack ids the lifespan leader installs (if not already present) when DEMO_MODE is on,
# via the existing on-demand registry installer — e.g. "masterpieces,impressionism". Empty = no boot
# bootstrap (an operator seeded the demo box by hand).
DEMO_PACKS = [p.strip() for p in os.getenv("SD_DEMO_PACKS", "").split(",") if p.strip()]

# Optional: the gallery `/` plays when demo mode is on (sets the `default_playlist` setting at boot,
# same key the Canvas already reads via /api/displays/{id}/preferred-playlist). Empty = leave whatever
# the installed pack(s) chose as default.
DEMO_DEFAULT_PLAYLIST = os.getenv("SD_DEMO_DEFAULT_PLAYLIST", "").strip()

# Concurrent demo WebSocket cap (core/demo.py) — a public box has no way to bound how many anonymous
# tabs open /ws/{display_id}, so this is a blunt ceiling; env-overridable for a beefier VPS.
DEMO_WS_MAX = int(os.getenv("SD_DEMO_WS_MAX", "200"))

# Shared secret gating the appliance update bridge (/api/appliance/update — the highest-consequence
# action: it can force a host git reset+rebuild or reboot). N6: fail CLOSED. The endpoint accepts EITHER
# a same-origin browser request (the Origin allowlist check in app.py) OR a request carrying a matching
# X-Appliance-Token header — a caller with neither (e.g. curl/any other LAN device, with no token set)
# is refused outright, not waved through as "prior behavior". install.sh and sd-update each mint a
# random token into .env on first run if one isn't already present, so an appliance box always ends up
# with one configured; a warning is logged only in the (unexpected) case none is set at request time.
APPLIANCE_UPDATE_TOKEN = os.getenv("SD_APPLIANCE_UPDATE_TOKEN", "").strip()
