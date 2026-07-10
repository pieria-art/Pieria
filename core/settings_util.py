"""Settings-table read/write helpers shared across nearly every settings route, the display
schedule resolver, and the catalog remote-override lookup.
"""

import json
import logging
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from config import SD_USER_AGENT
from models import SettingsModel

logger = logging.getLogger("artwork-display-api")


def _upsert_setting(db: Session, key: str, value: str):
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == key).first()
    if row:
        row.setting_value = value
    else:
        db.add(SettingsModel(setting_key=key, setting_value=value))


# --- R1-F2: Night & Quiet Hours (clock-driven brightness/warmth + quiet-hours panel power) ----------
# Gentle defaults, warm-shift ON, quiet-hours panel-off OFF (opt-in) so nothing blanks unexpectedly.
# One global schedule for v1; the resolver takes a display_id so per-display overrides can layer in later
# (dev-rule #4 hierarchy). The Canvas applies a GPU-cheap CSS overlay; the appliance drives HDMI-CEC.
SCHEDULE_SETTING_KEY = "display_schedule"
DEFAULT_SCHEDULE = {
    "enabled": True,
    "day_brightness": 1.0,     # 0.1..1.0 — screen brightness by day
    "night_brightness": 0.72,  # 0.1..1.0 — dimmed at full night
    "night_warmth": 0.28,      # 0..1 — amber tint strength at full night (0 = no colour shift)
    "evening_start": "20:00",  # begin the day -> night ramp
    "night_start": "22:30",    # fully night by here
    "morning_start": "06:30",  # begin the night -> day ramp
    "day_start": "08:00",      # fully day by here
    "quiet_enabled": False,    # opt-in: blank / power the panel off overnight
    "quiet_start": "23:30",
    "quiet_end": "07:00",
    "quiet_mode": "cec",       # "cec" (appliance powers panel) | "blackout" (software only)
}


def _load_schedule(db: Session) -> dict:
    """Stored schedule merged over the defaults (so new keys always have a value)."""
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == SCHEDULE_SETTING_KEY).first()
    stored = {}
    if row and row.setting_value:
        try:
            stored = json.loads(row.setting_value)
        except json.JSONDecodeError:
            logger.warning("display_schedule setting is not valid JSON — using defaults")
    return {**DEFAULT_SCHEDULE, **stored}


async def _catalog_remote_base(db: Session) -> Optional[str]:
    """Optional remote override: a static base URL hosting index.json + <id>.json (no server needed)."""
    setting = db.query(SettingsModel).filter(SettingsModel.setting_key == "catalog_url").first()
    return setting.setting_value.rstrip("/") if setting and setting.setting_value else None


async def _fetch_remote_json(base: str, name: str):
    async with httpx.AsyncClient(headers={"User-Agent": SD_USER_AGENT}) as client:
        r = await client.get(f"{base}/{name}", timeout=15.0, follow_redirects=True)
        if r.status_code == 200:
            return r.json()
    raise RuntimeError(f"HTTP {r.status_code}")
