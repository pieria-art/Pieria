"""Cache correctness (Phase 2): the thumbnail lru is mtime-keyed (A4) and the catalog JSON memo hands
back isolated copies (A2), so an in-place file replace is never served stale and a caller mutating a
returned catalog dict can't corrupt the shared cache."""

import asyncio
import json
import os
import tempfile
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
    core_media._optimized_image_lru.clear()
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


# --- M6 reviewer follow-ups (2026-09-25) --------------------------------------------------------------

def test_optimized_image_prunes_stale_sibling_on_mtime_change(tmp_path, monkeypatch):
    """A disk-cache entry keyed by mtime must not accumulate forever — an in-place edit/replace used
    to leave the OLD derivative orphaned on disk permanently (unbounded growth; a real concern on a
    Pi's SD card). get_optimized_image now prunes same (name, size, quality) siblings at a different
    mtime, same as render_canvas_image already did for the Canvas derivative."""
    monkeypatch.setattr(core_media, "DERIVATIVES_DIR", tmp_path / "_derivatives")
    p = tmp_path / "x.jpg"
    Image.new("RGB", (800, 600), (10, 20, 30)).save(p, "JPEG")
    os.utime(p, (1000, 1000))
    core_media.get_optimized_image(p, (400, 400), 70)
    first_files = list((tmp_path / "_derivatives").glob("opt-x.jpg-400x400-*"))
    assert len(first_files) == 1

    Image.new("RGB", (800, 600), (200, 100, 50)).save(p, "JPEG")
    os.utime(p, (2000, 2000))
    core_media._optimized_image_lru.clear()
    core_media.get_optimized_image(p, (400, 400), 70)

    files = list((tmp_path / "_derivatives").glob("opt-x.jpg-400x400-*"))
    assert len(files) == 1, f"orphaned derivative(s) left behind: {files}"
    assert files[0] != first_files[0]


def test_render_canvas_image_prunes_stale_sibling_on_mtime_change(tmp_path, monkeypatch):
    monkeypatch.setattr(core_media, "DERIVATIVES_DIR", tmp_path / "_derivatives")
    p = tmp_path / "c.jpg"
    Image.new("RGB", (800, 600), (10, 20, 30)).save(p, "JPEG")
    os.utime(p, (1000, 1000))
    core_media.render_canvas_image(p, art_id=42)
    assert len(list((tmp_path / "_derivatives").glob("42-*.jpg"))) == 1

    Image.new("RGB", (800, 600), (9, 9, 9)).save(p, "JPEG")
    os.utime(p, (2000, 2000))
    core_media.render_canvas_image(p, art_id=42)
    files = list((tmp_path / "_derivatives").glob("42-*.jpg"))
    assert len(files) == 1, f"orphaned Canvas derivative(s) left behind: {files}"


@pytest.mark.asyncio
async def test_run_image_work_cache_hit_skips_the_semaphore(monkeypatch):
    """A cache hit must return immediately WITHOUT ever taking the decode semaphore — otherwise a warm
    thumbnail queues behind someone else's cold render for no reason."""
    sem = asyncio.Semaphore(1)
    monkeypatch.setattr(core_media, "_image_decode_semaphore", sem)

    async def _hold_semaphore_forever():
        await sem.acquire()   # simulate a slow render already occupying the only slot

    await _hold_semaphore_forever()

    def _boom(*_a, **_k):
        raise AssertionError("fn was called — cache_check should have short-circuited")

    result = await core_media.run_image_work(_boom, cache_check=lambda: b"cached-bytes")
    assert result == b"cached-bytes"


@pytest.mark.asyncio
async def test_run_image_work_cache_miss_still_uses_the_semaphore(monkeypatch):
    monkeypatch.setattr(core_media, "_image_decode_semaphore", asyncio.Semaphore(1))
    result = await core_media.run_image_work(lambda: b"rendered", cache_check=lambda: None)
    assert result == b"rendered"


def test_atomic_write_uses_unique_per_call_tmp_name(tmp_path, monkeypatch):
    """M6 nit: mkstemp, not a PID-based tmp name — two THREADS in one worker process share a pid, so
    concurrent renders of the same key used to race on a shared .tmp path."""
    dst1 = tmp_path / "a.jpg"
    dst2 = tmp_path / "b.jpg"
    seen_names = []
    real_mkstemp = tempfile.mkstemp

    def _spy_mkstemp(*a, **k):
        fd, name = real_mkstemp(*a, **k)
        seen_names.append(name)
        return fd, name

    monkeypatch.setattr(core_media.tempfile, "mkstemp", _spy_mkstemp)
    core_media._atomic_write(dst1, b"one")
    core_media._atomic_write(dst2, b"two")

    assert len(seen_names) == len(set(seen_names))   # every call got its own unique tmp path
    assert dst1.read_bytes() == b"one"
    assert dst2.read_bytes() == b"two"
    assert list(tmp_path.glob("*.tmp")) == []
