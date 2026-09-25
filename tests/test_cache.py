"""Cache correctness (Phase 2): the thumbnail lru is mtime-keyed (A4) and the catalog JSON memo hands
back isolated copies (A2), so an in-place file replace is never served stale and a caller mutating a
returned catalog dict can't corrupt the shared cache."""

import asyncio
import json
import os
import threading
import time

import pytest
from PIL import Image

import app as app_module
import core.media as core_media


def test_optimized_image_busts_on_mtime_change(tmp_path):
    p = tmp_path / "x.jpg"
    Image.new("RGB", (800, 600), (10, 20, 30)).save(p, "JPEG")
    os.utime(p, (1000, 1000))
    before = core_media.get_optimized_image(p, (400, 400), 70)

    # Replace the file in place with a clearly different image + a newer mtime.
    Image.new("RGB", (800, 600), (200, 100, 50)).save(p, "JPEG")
    os.utime(p, (2000, 2000))
    after = core_media.get_optimized_image(p, (400, 400), 70)

    assert before != after   # mtime participates in the lru key → no stale thumbnail (A4)


def test_read_local_json_isolates_callers(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"collections": [{"id": "a"}]}))
    first = app_module._read_local_json(p)
    first["collections"].append({"id": "INJECTED"})   # mutate the returned object

    second = app_module._read_local_json(p)            # served from the mtime cache, but deep-copied
    assert [c["id"] for c in second["collections"]] == ["a"]   # caller mutation didn't leak (A2)


# --- M4: thumbnail/preview disk-cache tier (production incident, INFRA-D075 2026-09-25) -------------
# get_optimized_image's in-memory lru_cache is the first tier; a restart (or eviction past maxsize=256)
# used to mean re-decoding every original from scratch. A disk tier under _derivatives/ backs it, keyed
# exactly like render_canvas_image's Canvas derivative.

def test_optimized_image_disk_cache_survives_lru_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr(core_media, "DERIVATIVES_DIR", tmp_path / "_derivatives")
    p = tmp_path / "x.jpg"
    Image.new("RGB", (800, 600), (10, 20, 30)).save(p, "JPEG")
    os.utime(p, (1000, 1000))

    first = core_media.get_optimized_image(p, (400, 400), 70)
    mtime = int(p.stat().st_mtime)
    dst = core_media._optimized_derivative_path(p, (400, 400), 70, mtime)
    assert dst.exists()   # atomically published to disk on first render

    # Simulate a process restart: clear the in-memory lru so the next call must hit disk, not Pillow.
    core_media._optimized_image_cached.cache_clear()
    monkeypatch.setattr(Image, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-decoded")))
    second = core_media.get_optimized_image(p, (400, 400), 70)
    assert second == first


def test_optimized_image_disk_cache_atomic_write_leaves_no_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(core_media, "DERIVATIVES_DIR", tmp_path / "_derivatives")
    p = tmp_path / "y.jpg"
    Image.new("RGB", (400, 300), (5, 5, 5)).save(p, "JPEG")
    core_media.get_optimized_image(p, (200, 200), 70)
    leftovers = list((tmp_path / "_derivatives").glob("*.tmp"))
    assert leftovers == []


# --- M6: process-wide Pillow-decode concurrency bound (SD_IMAGE_WORKERS) -----------------------------

@pytest.mark.asyncio
async def test_run_image_work_bounds_concurrency(monkeypatch):
    monkeypatch.setattr(core_media, "_image_decode_semaphore", asyncio.Semaphore(2))
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def _slow(_i):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return _i

    results = await asyncio.gather(*[core_media.run_image_work(_slow, i) for i in range(6)])
    assert sorted(results) == list(range(6))
    assert max_in_flight <= 2   # never more than SD_IMAGE_WORKERS concurrent Pillow calls
