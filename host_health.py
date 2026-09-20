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
import time
from datetime import UTC, datetime

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


def _read_json(name):
    """One shape for every root-written JSON mailbox file in data/appliance/: the file may be absent
    (before the first host run), mid-write, or unreadable — all of which mean None, never an
    exception. The endpoint must not 500 because a timer hasn't fired yet."""
    path = config.APPLIANCE_DIR / name
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def read_conf():
    """The non-secret mirror of pieria.conf, exported by `sd-conf export` (written after every conf
    edit and self-healed by the sd-metrics timer). The app NEVER reads /boot/firmware itself — it is
    outside the container — so this file is how the admin UI knows the device's current settings."""
    return _read_json("conf.json")


def read_os_updates():
    """Last `sd-os-check` result: how many packages apt would install and whether that needs a
    reboot. None before the nightly timer has ever run."""
    return _read_json("os-updates.json")


def read_support_bundle():
    """Presence + age + size of the diagnostic tarball, so the admin UI can offer the download link
    and say how fresh it is. The bundle itself is served by its own endpoint."""
    path = config.APPLIANCE_DIR / "support-bundle.tar.gz"
    try:
        if not path.exists():
            return {"exists": False}
        st = path.stat()
        return {"exists": True,
                "created_at": datetime.fromtimestamp(st.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size_bytes": st.st_size}
    except OSError:
        return {"exists": False}


def read_last_update_log(max_lines: int = 60):
    """Tail of the last sd-update run. status.json only carries a live tail and is overwritten by the
    next action, so this is the only record of what the PREVIOUS action actually did — the thing you
    want after a reboot that ate the status."""
    path = config.APPLIANCE_DIR / "last-update.log"
    try:
        if path.exists():
            return path.read_text().splitlines()[-max_lines:]
    except OSError:
        pass
    return None


def container_tz():
    """The clock this PROCESS is on. ADR-118: the Pi sat on Europe/London while the container ran on
    UTC, so Night & Quiet Hours fired hours off and nothing in the UI could show the disagreement.
    Surfacing both halves is what makes that visible — compare against conf.json's host_timezone."""
    try:
        return {"tzname": list(time.tzname), "offset_s": -time.timezone,
                "now": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    except (OSError, ValueError):
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
        "conf": read_conf(),
        "os_updates": read_os_updates(),
        "support_bundle": read_support_bundle(),
        "last_update_log": read_last_update_log(),
        "container_tz": container_tz(),
    }
