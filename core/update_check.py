"""Update availability check (ADR-071) — "is there a newer release?" for the appliance.

The APPLY path already exists (`sd-update` → git checkout + rebuild, via routers/health.py). This is
the missing NOTIFY half: a self-updating box is useless if nobody knows there's anything to update.

Design (Josh's calls, 2026-07-23):
  * Channel = GitHub **Releases** (tagged, with notes) — not raw main. Users see meaningful, changelog'd
    updates, and `sd-update` checks out the tag. `config.APP_VERSION` is what this box believes it runs;
    the tag is what shipped; keep them in lockstep when cutting a release.
  * Notify only — this module never applies anything. It reports; the human clicks Update.

The check is a plain HTTPS GET to api.github.com (60/hr per IP unauthenticated — a daily background
refresh plus the odd manual click is nowhere near that). Results cache to a file so the admin UI reads a
cheap local value and GitHub is not hit on every page load. Everything is best-effort: a failed check
reports `error` and never raises, because "couldn't reach GitHub" must never break the admin page.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime

import httpx

import config

logger = logging.getLogger("artwork-display-api")

_CACHE = config.APPLIANCE_DIR / "update-check.json"
_MIN_REFRESH_SEC = 900          # never hit GitHub more than once per 15 min, even on a manual "check now"
_RELEASES_URL = f"https://api.github.com/repos/{config.UPDATE_REPO}/releases/latest"
_UA = "ScreenDocent-UpdateCheck"


def _parse_version(s: str) -> tuple | None:
    """A comparable tuple from a version/tag string, or None if it isn't parseable.

    Tolerant of a leading `v` and any pre-release/build suffix (`1.2.0-rc1` → (1,2,0)). Returning None
    on garbage is deliberate: an unparseable version means "don't claim an update," never a crash or a
    false positive."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", (s or "").strip())
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def _is_newer(latest: str, current: str) -> bool:
    """True only when we can PARSE BOTH and latest > current. Unknown → False (never nag on doubt)."""
    lv, cv = _parse_version(latest), _parse_version(current)
    if lv is None or cv is None:
        return False
    return lv > cv


def _read_cache() -> dict | None:
    try:
        return json.loads(_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(payload: dict) -> None:
    try:
        config.APPLIANCE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(payload))
    except OSError as e:
        logger.warning(f"[update-check] could not cache result: {e}")


async def _fetch_latest_release() -> dict:
    """GET the latest GitHub Release. Returns the fields we surface, or {} on any failure (no releases
    yet → GitHub 404, which is a normal pre-launch state, not an error to alarm about)."""
    async with httpx.AsyncClient(headers={"User-Agent": _UA, "Accept": "application/vnd.github+json"},
                                 timeout=15, follow_redirects=True) as client:
        r = await client.get(_RELEASES_URL)
        if r.status_code == 404:
            return {"none": True}          # repo has no published releases yet
        r.raise_for_status()
        j = r.json()
        return {
            "tag": j.get("tag_name") or "",
            "name": j.get("name") or j.get("tag_name") or "",
            "notes": (j.get("body") or "").strip(),
            "url": j.get("html_url") or "",
            "published_at": j.get("published_at") or "",
        }


def _result(current: str, rel: dict, error: str | None = None) -> dict:
    """Shape the cached/returned status from a release payload."""
    tag = rel.get("tag", "")
    update_available = bool(tag) and _is_newer(tag, current)
    return {
        "current": current,
        "latest": tag,
        "latest_name": rel.get("name", ""),
        "notes": rel.get("notes", ""),
        "url": rel.get("url", ""),
        "update_available": update_available,
        "no_releases": bool(rel.get("none")),
        "checked_at": datetime.now(UTC).isoformat(),
        "error": error,
    }


async def check_for_update(force: bool = False) -> dict:
    """Return the current update status, using the cache unless `force` (and even then, not more often
    than every 15 min). Never raises."""
    cached = _read_cache()
    if cached and not force:
        return cached
    # Rate-limit even forced checks so a user hammering "check now" can't spam GitHub.
    if cached and force:
        try:
            age = time.time() - datetime.fromisoformat(cached["checked_at"]).timestamp()
            if age < _MIN_REFRESH_SEC:
                return {**cached, "throttled": True}
        except (KeyError, ValueError):
            pass
    try:
        rel = await _fetch_latest_release()
        result = _result(config.APP_VERSION, rel)
    except Exception as e:  # noqa: BLE001 — a failed check must degrade, never break the admin page
        logger.info(f"[update-check] check failed: {type(e).__name__}: {e}")
        # Keep the last good result if we have one; just annotate the failure + when we tried.
        base = cached or _result(config.APP_VERSION, {})
        result = {**base, "error": f"{type(e).__name__}: {e}", "checked_at": datetime.now(UTC).isoformat()}
    _write_cache(result)
    return result
