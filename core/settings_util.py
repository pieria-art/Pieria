"""Settings-table read/write helpers shared across nearly every settings route, the display
schedule resolver, and the catalog remote-override lookup.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from config import SD_USER_AGENT
from core import safe_http
from core.downloads import guarded_stream
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


def _resolve_scheduled(schedule: dict, now: datetime) -> dict:
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



# --- A2 (ADR-147): manual quiet override ---------------------------------------------------------------
# "Art off when we leave / on when we're home". One global override in the Settings KV, layered over the
# schedule by resolve_schedule_state, so the Canvas blackout AND the appliance CEC timer (both poll
# schedule-state) honour it with no other change. Stored as JSON {"on": bool, "until": epoch|None}.
# Tri-state at the API: mode on (force quiet) / off (force awake) / auto (no override).
#   on / off -> ends at `until` or at the NEXT scheduled quiet boundary, whichever is first, so a forced
#               state never sticks silently. (The expiry is resolved once, at set time, and stored — so
#               "until" is always honest.) With no quiet schedule there is no boundary: indefinite.
#   auto     -> the override is deleted; the schedule rules.
# An expired override is deleted the next time it is read.
QUIET_OVERRIDE_KEY = "quiet_override"


def _schedule_quiet_window(schedule: dict, now: datetime) -> tuple[bool, Optional[datetime]]:
    """(is the SCHEDULE quiet now, when that flips next as an aware-UTC datetime or None). `now` is the
    server's local wall clock (naive), like resolve_schedule_state's."""
    s = {**DEFAULT_SCHEDULE, **(schedule or {})}
    if not s.get("enabled", True) or not s.get("quiet_enabled"):
        return False, None
    qs = _parse_hhmm(s["quiet_start"], 1410)
    qe = _parse_hhmm(s["quiet_end"], 420)
    if _cyc_len(qs, qe) == 0:
        return False, None
    t = now.hour * 60 + now.minute
    active = _cyc_in(t, qs, qe)
    edge = qe if active else qs
    delta = (edge - t) % 1440 or 1440
    flip = now.replace(second=0, microsecond=0) + timedelta(minutes=delta)
    return active, flip.astimezone(UTC)   # naive == local wall clock


def get_quiet_override(db: Session, now_utc: Optional[datetime] = None) -> Optional[dict]:
    """The live override as {"on": bool, "until": aware-UTC datetime | None}, or None. Clears an expired one."""
    now_utc = now_utc or datetime.now(UTC)
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == QUIET_OVERRIDE_KEY).first()
    if row is None:
        return None
    try:
        data = json.loads(row.setting_value)
        on = bool(data["on"])
        until = datetime.fromtimestamp(float(data["until"]), UTC) if data.get("until") is not None else None
    except (ValueError, KeyError, TypeError):
        db.delete(row)
        db.commit()
        return None
    if until is not None and until <= now_utc:
        db.delete(row)
        db.commit()
        return None
    return {"on": on, "until": until}


def set_quiet_override(db: Session, schedule: dict, on: Optional[bool], until: Optional[datetime],
                       now_local: Optional[datetime] = None) -> None:
    """Persist an override (see the block comment for the expiry rule); `on=None` clears it (auto).
    `until` must be aware."""
    if on is None:
        db.query(SettingsModel).filter(SettingsModel.setting_key == QUIET_OVERRIDE_KEY).delete()
        db.commit()
        return
    _active, flip = _schedule_quiet_window(schedule, now_local or datetime.now())
    expiry = until
    if flip is not None and (expiry is None or flip < expiry):
        expiry = flip
    _upsert_setting(db, QUIET_OVERRIDE_KEY,
                    json.dumps({"on": on, "until": expiry.timestamp() if expiry else None}))
    db.commit()


def quiet_status(schedule: dict, override: Optional[dict], now_local: Optional[datetime] = None) -> dict:
    """{"active", "mode": on|off|auto, "source": schedule|manual|none, "until": aware-UTC datetime|None}."""
    if override is not None:
        return {"active": override["on"], "mode": "on" if override["on"] else "off", "source": "manual",
                "until": override["until"]}
    active, flip = _schedule_quiet_window(schedule, now_local or datetime.now())
    if active:
        return {"active": True, "mode": "auto", "source": "schedule", "until": flip}
    return {"active": False, "mode": "auto", "source": "none", "until": None}


def resolve_schedule_state(schedule: dict, now: datetime, override: Optional[dict] = None) -> dict:
    """The schedule's brightness/warmth/quiet for `now`, with an optional manual quiet `override`
    (from get_quiet_override) forcing `quiet` either way. Brightness/warmth are unaffected."""
    state = _resolve_scheduled(schedule, now)
    if override is not None:
        state["quiet"] = bool(override["on"])
    return state

_HHMM_RE = re.compile(r"^\d{1,2}:\d{2}$")


class ScheduleError(ValueError):
    """A schedule value failed validation; the message names the field and the rule."""


def apply_schedule_patch(db: Session, changes: dict) -> dict:
    """Merge `changes` (already None-stripped) over the stored schedule, validate, persist, return the
    merged schedule. The ONE validator for the display schedule — the internal settings route and the
    public API's PATCH /schedule both call it. Raises ScheduleError (nothing is written on failure)."""
    merged = _load_schedule(db)
    merged.update(changes)
    # Validate ranges/formats so a bad value can't wedge the resolver or the Canvas overlay.
    for bkey in ("day_brightness", "night_brightness"):
        if not (0.1 <= float(merged[bkey]) <= 1.0):
            raise ScheduleError(f"{bkey} must be between 0.1 and 1.0")
    if not (0.0 <= float(merged["night_warmth"]) <= 1.0):
        raise ScheduleError("night_warmth must be between 0.0 and 1.0")
    for tkey in ("evening_start", "night_start", "morning_start", "day_start", "quiet_start", "quiet_end"):
        if not _HHMM_RE.match(str(merged[tkey])):
            raise ScheduleError(f"{tkey} must be HH:MM")
    if merged["quiet_mode"] not in ("cec", "blackout"):
        raise ScheduleError("quiet_mode must be 'cec' or 'blackout'")
    _upsert_setting(db, SCHEDULE_SETTING_KEY, json.dumps(merged))
    db.commit()
    return merged


async def _catalog_remote_base(db: Session) -> Optional[str]:
    """Optional remote override: a static base URL hosting index.json + <id>.json (no server needed)."""
    setting = db.query(SettingsModel).filter(SettingsModel.setting_key == "catalog_url").first()
    return setting.setting_value.rstrip("/") if setting and setting.setting_value else None


async def _fetch_remote_json(base: str, name: str):
    """GET `<base>/<name>` from the admin-settable `catalog_url` override. N5: `base` is arbitrary
    user/admin input with no SSRF guard before this fix, and `follow_redirects=True` let a redirect
    bypass any guard added at the entry point — routed through guarded_stream (core/pack_fetch.py's
    shared helper), which SSRF-validates the URL and every redirect hop before following it."""
    async with safe_http.safe_async_client(headers={"User-Agent": SD_USER_AGENT}) as client:
        async with guarded_stream(client, "GET", f"{base}/{name}", timeout=15.0) as r:
            if r.status_code == 200:
                await r.aread()
                return r.json()
            raise RuntimeError(f"HTTP {r.status_code}")
