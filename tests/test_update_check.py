"""Update availability check (ADR-071): compare config.APP_VERSION to the latest GitHub Release, cache
the result, and NEVER raise on a failed check — a box that can't reach GitHub must still serve admin."""
import json

import pytest

import config
from core import update_check as uc


@pytest.mark.parametrize("s,expected", [
    ("1.2.3", (1, 2, 3)), ("v1.2.3", (1, 2, 3)), ("0.4.5", (0, 4, 5)),
    ("v2", (2,)), ("1.2.0-rc1", (1, 2, 0)), ("v1.0.0-beta.2", (1, 0, 0)),
    ("garbage", None), ("", None), ("v", None),
])
def test_parse_version(s, expected):
    assert uc._parse_version(s) == expected


@pytest.mark.parametrize("latest,current,newer", [
    ("v1.1.0", "1.0.0", True),
    ("v1.0.1", "1.0.0", True),
    ("v1.0.0", "1.0.0", False),      # equal is not newer
    ("v0.9.0", "1.0.0", False),      # older is not newer
    ("v2.0", "1.9.9", True),
    ("garbage", "1.0.0", False),     # unparseable latest -> never claim an update
    ("v1.1.0", "garbage", False),    # unparseable current -> never claim an update
])
def test_is_newer_never_nags_on_doubt(latest, current, newer):
    assert uc._is_newer(latest, current) is newer


@pytest.fixture
def appliance_dir(tmp_path, monkeypatch):
    d = tmp_path / "appliance"
    monkeypatch.setattr(config, "APPLIANCE_DIR", d)
    monkeypatch.setattr(uc, "_CACHE", d / "update-check.json")
    return d


async def _fake_release(payload):
    async def _f():
        return payload
    return _f


@pytest.mark.asyncio
async def test_check_reports_update_available(appliance_dir, monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(uc, "_fetch_latest_release", await _fake_release(
        {"tag": "v1.2.0", "name": "1.2.0 — Faster e-ink", "notes": "* bugfixes", "url": "https://x/r"}))
    r = await uc.check_for_update(force=True)
    assert r["update_available"] is True
    assert r["latest"] == "v1.2.0"
    assert r["current"] == "1.0.0"
    assert "bugfixes" in r["notes"]
    # ...and it cached
    assert json.loads((appliance_dir / "update-check.json").read_text())["latest"] == "v1.2.0"


@pytest.mark.asyncio
async def test_check_reports_up_to_date(appliance_dir, monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "1.2.0")
    monkeypatch.setattr(uc, "_fetch_latest_release", await _fake_release(
        {"tag": "v1.2.0", "name": "1.2.0", "notes": "", "url": ""}))
    r = await uc.check_for_update(force=True)
    assert r["update_available"] is False


@pytest.mark.asyncio
async def test_no_releases_is_not_an_error(appliance_dir, monkeypatch):
    """Pre-launch, the repo has no releases -> GitHub 404 -> {none:True}. Reported as 'no releases',
    not as a failure, and never as an available update."""
    monkeypatch.setattr(config, "APP_VERSION", "0.4.5")
    monkeypatch.setattr(uc, "_fetch_latest_release", await _fake_release({"none": True}))
    r = await uc.check_for_update(force=True)
    assert r["update_available"] is False
    assert r["no_releases"] is True
    assert r["error"] is None


@pytest.mark.asyncio
async def test_a_failed_check_degrades_and_keeps_the_last_good_result(appliance_dir, monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(uc, "_MIN_REFRESH_SEC", 0)   # let the second forced check actually re-fetch
    # First: a good check populates the cache with an available update.
    monkeypatch.setattr(uc, "_fetch_latest_release", await _fake_release(
        {"tag": "v1.3.0", "name": "1.3.0", "notes": "notes", "url": "u"}))
    good = await uc.check_for_update(force=True)
    assert good["update_available"] is True

    # Then GitHub goes unreachable. The check must NOT raise, and should retain the known-good info.
    async def boom():
        raise RuntimeError("dns fail")
    monkeypatch.setattr(uc, "_fetch_latest_release", boom)
    degraded = await uc.check_for_update(force=True)
    assert degraded["latest"] == "v1.3.0"          # last good info kept
    assert degraded["update_available"] is True
    assert "RuntimeError" in degraded["error"]     # but the failure is surfaced


@pytest.mark.asyncio
async def test_forced_check_is_throttled_so_a_button_masher_cant_spam_github(appliance_dir, monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "1.0.0")
    calls = {"n": 0}

    async def counting():
        calls["n"] += 1
        return {"tag": "v1.1.0", "name": "1.1.0", "notes": "", "url": ""}
    monkeypatch.setattr(uc, "_fetch_latest_release", counting)

    await uc.check_for_update(force=True)          # real call
    r = await uc.check_for_update(force=True)       # within 15 min -> throttled, served from cache
    assert calls["n"] == 1
    assert r.get("throttled") is True


@pytest.mark.asyncio
async def test_unforced_check_reads_the_cache_without_hitting_github(appliance_dir, monkeypatch):
    (appliance_dir).mkdir(parents=True, exist_ok=True)
    (appliance_dir / "update-check.json").write_text(json.dumps(
        {"current": "1.0.0", "latest": "v1.0.0", "update_available": False, "checked_at": "2026-01-01T00:00:00+00:00"}))

    async def must_not_call():
        raise AssertionError("unforced check must not hit GitHub")
    monkeypatch.setattr(uc, "_fetch_latest_release", must_not_call)
    r = await uc.check_for_update(force=False)
    assert r["latest"] == "v1.0.0"
