"""Offline sunrise/sunset for the schedule's "follow the sun" mode (ADR-148).

Pure Python, no dependency: the NOAA solar-position algorithm (the spreadsheet Meeus approximation,
good to ~1 min between roughly +/-72 deg latitude) plus a location lookup that defaults to the current
time zone's tzdata reference coordinates (zone.tab / zone1970.tab, ISO 6709), so a Pi with a TIMEZONE
set needs no manual lat/long. Nothing here touches the network.
"""

import logging
import math
import os
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger("artwork-display-api")

# Official zenith for sunrise/sunset: 90 deg + 50' (refraction + solar radius).
_ZENITH = 90.833


def sun_times_utc_minutes(day: date, lat: float, lon: float) -> Optional[tuple[float, float]]:
    """(sunrise, sunset) as minutes after 00:00 UTC of `day` (may fall outside 0..1440 for far-east/west
    longitudes), or None when the sun never crosses the horizon that day (polar day / polar night)."""
    def at(sunrise: bool) -> Optional[float]:
        # NOAA: estimate the event time, then refine using the solar position at that estimate.
        t_min = 720.0 - 4.0 * lon
        for _ in range(3):
            jd = day.toordinal() + 1721424.5 + t_min / 1440.0
            T = (jd - 2451545.0) / 36525.0
            l0 = (280.46646 + T * (36000.76983 + T * 0.0003032)) % 360
            m = 357.52911 + T * (35999.05029 - 0.0001537 * T)
            mr = math.radians(m)
            e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
            c = (math.sin(mr) * (1.914602 - T * (0.004817 + 0.000014 * T))
                 + math.sin(2 * mr) * (0.019993 - 0.000101 * T) + math.sin(3 * mr) * 0.000289)
            true_long = l0 + c
            omega = 125.04 - 1934.136 * T
            app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
            obliq = (23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60
                     + 0.00256 * math.cos(math.radians(omega)))
            decl = math.degrees(math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(app_long))))
            y = math.tan(math.radians(obliq / 2)) ** 2
            l0r = math.radians(l0)
            eq_time = 4 * math.degrees(
                y * math.sin(2 * l0r) - 2 * e * math.sin(mr) + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
                - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
            lr, dr = math.radians(lat), math.radians(decl)
            cos_ha = math.cos(math.radians(_ZENITH)) / (math.cos(lr) * math.cos(dr)) - math.tan(lr) * math.tan(dr)
            if cos_ha > 1 or cos_ha < -1:
                return None
            ha = math.degrees(math.acos(cos_ha))
            t_min = 720.0 - 4.0 * (lon + (ha if sunrise else -ha)) - eq_time
        return t_min

    rise, sset = at(True), at(False)
    if rise is None or sset is None:
        return None
    return rise, sset


def sun_times_local(day: date, lat: float, lon: float, utc_offset_min: float) -> Optional[tuple[int, int]]:
    """(sunrise, sunset) as local minutes since midnight (0..1439), or None for polar day/night."""
    r = sun_times_utc_minutes(day, lat, lon)
    if r is None:
        return None
    return int(round(r[0] + utc_offset_min)) % 1440, int(round(r[1] + utc_offset_min)) % 1440


# --- location from the time zone -------------------------------------------------------------------

_ZONE_TAB_DIRS = ("/usr/share/zoneinfo", "/usr/share/lib/zoneinfo", "/usr/lib/zoneinfo")
_ISO6709 = re.compile(r"^([+-])(\d{2})(\d{2})(\d{2})?([+-])(\d{3})(\d{2})(\d{2})?$")


def parse_iso6709(s: str) -> Optional[tuple[float, float]]:
    """'+513030-0000731' / '+4852+00220' -> (lat, lon) decimal degrees."""
    m = _ISO6709.match(s.strip())
    if not m:
        return None
    ls, ld, lm, lsec, os_, od, om, osec = m.groups()
    lat = int(ld) + int(lm) / 60 + int(lsec or 0) / 3600
    lon = int(od) + int(om) / 60 + int(osec or 0) / 3600
    return (-lat if ls == "-" else lat), (-lon if os_ == "-" else lon)


def _tab_paths() -> list[Path]:
    dirs = [Path(d) for d in _ZONE_TAB_DIRS]
    try:
        import tzdata  # the pip package, bundled in the image (requirements.txt)
        dirs.append(Path(tzdata.__file__).parent / "zoneinfo")
    except ImportError:
        pass
    # zone.tab lists every zone name (incl. links like Atlantic/Reykjavik); zone1970.tab is the fallback.
    return [d / n for n in ("zone.tab", "zone1970.tab") for d in dirs]


@lru_cache(maxsize=1)
def _zone_table() -> dict[str, tuple[float, float]]:
    table: dict[str, tuple[float, float]] = {}
    for path in reversed(_tab_paths()):   # earlier (preferred) files are applied last, so they win
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 3:
                coords = parse_iso6709(cols[1])
                if coords:
                    table[cols[2]] = coords
    return table


def zone_coords(zone: Optional[str]) -> Optional[tuple[float, float]]:
    """Reference (lat, lon) for an IANA zone name, or None if unknown/unlisted."""
    return _zone_table().get(zone) if zone else None


def current_zone() -> Optional[str]:
    """The house time zone: conf.json TIMEZONE (the set-timezone bridge) > $TZ > /etc/localtime link."""
    try:
        import host_health
        conf = host_health.read_conf() or {}
        tz = conf.get("TIMEZONE") or conf.get("timezone")
        if tz:
            return str(tz)
    except Exception:  # noqa: BLE001 — a bad mailbox file must never break the schedule
        pass
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in target:
            return target.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    return None


def resolve_location(schedule: dict) -> Optional[tuple[float, float, str]]:
    """(lat, lon, source) — the schedule's manual lat/long if set, else the zone's reference coordinates
    ('zone:<name>'), else None."""
    lat, lon = schedule.get("latitude"), schedule.get("longitude")
    if lat is not None and lon is not None:
        return float(lat), float(lon), "manual"
    zone = current_zone()
    c = zone_coords(zone)
    return (c[0], c[1], f"zone:{zone}") if c else None


def _local_offset_min(day: date) -> float:
    """UTC offset of the house clock at local noon of `day` (the container runs on the house clock)."""
    return datetime(day.year, day.month, day.day, 12).astimezone().utcoffset().total_seconds() / 60


@lru_cache(maxsize=8)
def _cached_sun(day: date, lat: float, lon: float, off: float):
    return sun_times_local(day, lat, lon, off)


def todays_sun(schedule: dict, day: date) -> Optional[dict]:
    """{sunrise, sunset (minutes), lat, lon, source} for `day`, or None when there is no location or the
    sun does not rise/set that day. Cached per (day, location, zone offset) — computed once a day."""
    loc = resolve_location(schedule)
    if loc is None:
        return None
    lat, lon, source = loc
    t = _cached_sun(day, round(lat, 4), round(lon, 4), _local_offset_min(day))
    return None if t is None else {"sunrise": t[0], "sunset": t[1], "lat": lat, "lon": lon, "source": source}


def fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
