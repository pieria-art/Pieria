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
APP_VERSION = "0.4.5"

# owner/repo whose GitHub Releases define "latest". Public info; overridable for a fork.
UPDATE_REPO = os.getenv("SD_UPDATE_REPO", "AiwendilInTheWoods/Pieria").strip()

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
SD_USER_AGENT = "Pieria/1.0 (https://github.com/AiwendilInTheWoods/Pieria; art display) httpx"

# Modular-pack registry (ADR-040 #4 / ADR-038 §5): the public packs.json the "browse & download packs"
# card reads to offer on-demand collections. Default = the official Cloudflare R2 host behind curwe.ai;
# overridable per-install via the `pack_registry_url` setting (e.g. to point at a staging registry).
PACK_REGISTRY_URL = os.getenv("SD_PACK_REGISTRY_URL", "https://packs.curwe.ai/packs.json")

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

# Optional shared secret gating the appliance update bridge (/api/appliance/update — the highest-
# consequence action: it can force a host git reset+rebuild or reboot). Require-if-set: when this env
# var is present the endpoint demands a matching X-Appliance-Token header; when absent it falls back to
# the prior behavior (the Origin guard still blocks the hostile-browser-tab vector) and logs a warning.
APPLIANCE_UPDATE_TOKEN = os.getenv("SD_APPLIANCE_UPDATE_TOKEN", "").strip()
