"""Settings-table read/write helpers shared across nearly every settings route, the display
schedule resolver, and the catalog remote-override lookup.
"""

import json
import logging
import re
from datetime import datetime
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


def _parse_hhmm(value: str, fallback: int = 0) -> int:
    """'HH:MM' -> minutes since midnight (0..1439); tolerant, clamps, falls back on garbage."""
    try:
        h, m = str(value).split(":")
        return (int(h) % 24) * 60 + (int(m) % 60)
    except (ValueError, AttributeError):
        return fallback


def _cyc_len(a: int, b: int) -> int:
    """Clockwise minute span from a to b on a 24h dial (a==b -> full 1440-min day is treated as 0)."""
    return (b - a) % 1440


def _cyc_in(t: int, a: int, b: int) -> bool:
    """Is minute t within the clockwise window [a, b) — handles windows that wrap past midnight."""
    span = _cyc_len(a, b)
    return span > 0 and (t - a) % 1440 < span


def _cyc_frac(t: int, a: int, b: int) -> float:
    """Fraction (0..1) of the clockwise window [a, b) elapsed at minute t (wrap-safe)."""
    span = _cyc_len(a, b)
    return 0.0 if span == 0 else ((t - a) % 1440) / span


def resolve_schedule_state(schedule: dict, now: datetime) -> dict:
    """Pure: given the schedule config + a wall-clock time, return what the display should look like NOW.

    Returns {enabled, brightness (0.1..1), warmth (0..1), quiet (bool), quiet_mode}. 'night factor' n
    ramps 0 (day) -> 1 (night) across the evening window, holds at 1 overnight, and ramps back down over
    the morning window; brightness/warmth interpolate on n. Disabled -> fully neutral, no quiet.
    """
    s = {**DEFAULT_SCHEDULE, **(schedule or {})}
    if not s.get("enabled", True):
        return {"enabled": False, "brightness": 1.0, "warmth": 0.0, "quiet": False, "quiet_mode": s.get("quiet_mode", "cec")}

    t = now.hour * 60 + now.minute
    day_start = _parse_hhmm(s["day_start"], 480)
    evening = _parse_hhmm(s["evening_start"], 1200)
    night = _parse_hhmm(s["night_start"], 1350)
    morning = _parse_hhmm(s["morning_start"], 390)

    if _cyc_in(t, day_start, evening):
        n = 0.0
    elif _cyc_in(t, evening, night):
        n = _cyc_frac(t, evening, night)          # rising: day -> night
    elif _cyc_in(t, night, morning):
        n = 1.0                                    # night plateau (wraps midnight)
    elif _cyc_in(t, morning, day_start):
        n = 1.0 - _cyc_frac(t, morning, day_start)  # falling: night -> day
    else:
        n = 0.0                                    # windows didn't tile (misconfig) — default to day

    n = max(0.0, min(1.0, n))
    day_b = float(s["day_brightness"])
    night_b = float(s["night_brightness"])
    brightness = round(day_b + (night_b - day_b) * n, 4)
    warmth = round(float(s["night_warmth"]) * n, 4)

    quiet = bool(s.get("quiet_enabled")) and _cyc_in(
        t, _parse_hhmm(s["quiet_start"], 1410), _parse_hhmm(s["quiet_end"], 420))

    return {"enabled": True, "brightness": brightness, "warmth": warmth,
            "quiet": quiet, "quiet_mode": s.get("quiet_mode", "cec")}


_HHMM_RE = re.compile(r"^\d{1,2}:\d{2}$")


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
