"""
Host-health readers for the Device Health console (all-in-one appliance only).

Everything here is best-effort and stdlib-only: each reader returns None / "unavailable" on any
error so the endpoint never 500s and the dev/CI box (no /sys/class/thermal, no vcgencmd) exercises
the graceful-degrade path. Most metrics come straight from /proc and /sys, which Docker exposes at
host level without privilege. The one exception is the Pi throttle/under-voltage bitmask, which is
ONLY available via `vcgencmd` — a host binary backed by /dev/vcio that does not exist inside the
unprivileged container. So throttle is read from a small JSON file the host-side `sd-metrics` timer
writes (data/appliance/host_metrics.json); we fall back to attempting vcgencmd directly (works only
when this process happens to run on the host), else report "unavailable".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import config

# Raspberry Pi `vcgencmd get_throttled` bitmask. Low bits = condition active NOW; the same condition
# shifted up by 16 = "has occurred since boot". https://www.raspberrypi.com/documentation
_THROTTLE_BITS = {
    0: "under-voltage",
    1: "arm-frequency-capped",
    2: "currently-throttled",
    3: "soft-temperature-limit",
}


def decode_throttled(bits: int) -> dict:
    """Decode a vcgencmd throttled bitmask into active + occurred-since-boot condition lists.

    Pure function — the unit-test target. `bits` is the integer value (e.g. 0x50005)."""
    active = [name for shift, name in _THROTTLE_BITS.items() if bits & (1 << shift)]
    occurred = [name for shift, name in _THROTTLE_BITS.items() if bits & (1 << (shift + 16))]
    return {"raw": f"0x{bits:X}", "active": active, "occurred": occurred}


def read_loadavg() -> list | None:
    try:
        return list(os.getloadavg())
    except OSError:
        return None


def read_temp_c() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            return round(int(fh.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def read_memory() -> dict | None:
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])  # kB
        total = info["MemTotal"]
        available = info.get("MemAvailable", info.get("MemFree", 0))
        return {
            "total_mb": round(total / 1024),
            "available_mb": round(available / 1024),
            "used_pct": round((total - available) / total * 100, 1) if total else None,
        }
    except (OSError, ValueError, KeyError, IndexError):   # C6: a malformed meminfo line must not raise
        return None


def read_uptime_s() -> float | None:
    try:
        with open("/proc/uptime") as fh:
            return round(float(fh.read().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return None


def read_disk(path: str = "/app/data") -> dict | None:
    target = path if os.path.exists(path) else "/"
    try:
        total, used, free = shutil.disk_usage(target)
        return {
            "total_gb": round(total / 1024**3, 1),
            "free_gb": round(free / 1024**3, 1),
            "used_pct": round(used / total * 100, 1) if total else None,
        }
    except OSError:
        return None


def _read_vcgencmd_throttled() -> int | None:
    """Best-effort direct vcgencmd call. Returns the int bitmask or None when unavailable."""
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode != 0:
            return None
        # Output looks like: throttled=0x50005
        _, _, hexval = out.stdout.strip().partition("=")
        return int(hexval, 16)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read_throttled():
    """Throttle/under-voltage state. Prefer the host-writer file; fall back to vcgencmd; else
    the string "unavailable" (the in-container / dev / CI case)."""
    metrics_file = config.APPLIANCE_DIR / "host_metrics.json"
    try:
        if metrics_file.exists():
            data = json.loads(metrics_file.read_text())
            raw = data.get("throttled")
            if raw is not None:
                bits = int(raw, 16) if isinstance(raw, str) else int(raw)
                return decode_throttled(bits)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    bits = _read_vcgencmd_throttled()
    if bits is not None:
        return decode_throttled(bits)
    return "unavailable"


def read_watchdog():
    """Last self-heal snapshot written by the host-side sd-watchdog timer (all-in-one). Shows the mode
    (observe/enforce), the last probe result, and any action taken. None off-Pi / before the first run."""
    wd_file = config.APPLIANCE_DIR / "watchdog.json"
    try:
        if wd_file.exists():
            return json.loads(wd_file.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def collect() -> dict:
    """Assemble a single JSON-able health snapshot. Never raises."""
    return {
        "loadavg": read_loadavg(),
        "temp_c": read_temp_c(),
        "memory": read_memory(),
        "uptime_s": read_uptime_s(),
        "disk": read_disk(),
        "throttled": read_throttled(),
        "watchdog": read_watchdog(),
    }
