"""
Shared configuration constants for the Screen Docent application.
Extracted from app.py to break circular import dependencies.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ARTWORK_ROOT = Path(os.getenv("ARTWORK_ROOT", "Artwork"))
LIBRARY_DIR = ARTWORK_ROOT / "_Library"

# Wikimedia (and most museum/image hosts) reject the default httpx User-Agent; every outbound image
# fetch must send this descriptive UA. Lives here (not app.py) so the offline tools/ scripts can
# reuse it without importing the FastAPI app.
SD_USER_AGENT = "ScreenDocent/1.0 (https://github.com/AiwendilInTheWoods/Screen-Docent; art display) httpx"

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
