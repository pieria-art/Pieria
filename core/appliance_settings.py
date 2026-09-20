"""Appliance device settings — the container's half of the ADR-119 bridge.

The HOST script `deploy/appliance/bin/sd-conf` owns the validators and the conf writer. Rather than
re-implement them here (two copies drift, and the drift would be a security control), the app LOADS
that file: `deploy/` ships inside the image (.dockerignore excludes tests/tools/.ai, not deploy), so
the endpoint and the root helper validate through literally the same code.

The endpoint is the first gate and sd-conf on the host is the authoritative second one (the ADR-071
`ref` pattern). This module exists so the first gate can reject a bad value with a useful message
instead of queueing work that will fail silently three seconds later on the host.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger("artwork-display-api")

_SD_CONF = Path(__file__).resolve().parents[1] / "deploy" / "appliance" / "bin" / "sd-conf"


def _load():
    """Import sd-conf by path. Returns None when it isn't there — a non-appliance deployment, or a
    trimmed image — so the endpoint degrades to 'settings unavailable' rather than failing to start."""
    try:
        loader = importlib.machinery.SourceFileLoader("sd_conf", str(_SD_CONF))
        spec = importlib.util.spec_from_loader("sd_conf", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    except (OSError, SyntaxError, ImportError) as e:
        logger.warning(f"sd-conf not loadable ({e}) — appliance settings validation is unavailable")
        return None


sd_conf = _load()

AVAILABLE = sd_conf is not None


def validate(key: str, value: str):
    """Error message, or None. Without sd-conf we refuse everything: failing closed is the only safe
    direction for a value that ends up in a `.`-sourced shell file."""
    if sd_conf is None:
        return "settings validation unavailable on this build"
    return sd_conf.validate(key, value)


def sanitize_display_id(raw: str) -> str:
    return sd_conf.sanitize_display_id(raw) if sd_conf else ""


#: action -> [(request field, conf key, required)]. The endpoint copies ONLY these fields into
#: request.json, so a crafted request can't smuggle an extra key past the host's `case` arm.
ACTION_FIELDS = {
    "set-timezone":        [("timezone", "TIMEZONE", True)],
    "set-orientation":     [("orientation", "ROTATE", True)],
    "preview-orientation": [("orientation", "ROTATE", True)],
    "set-display-name":    [("display_id", "DISPLAY_ID", True)],
    "set-watchdog":        [("watchdog", "WATCHDOG", True)],
    "set-os-schedule":     [("schedule", "OS_UPDATE_SCHEDULE", True),
                            ("time", "OS_UPDATE_TIME", False)],
}
