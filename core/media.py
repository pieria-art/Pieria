"""Derivative-image rendering primitives, shared across every domain that serves or
creates an artwork image: the display feed (`display.jpg`), the admin/library grid
(thumbnail/preview), studio uploads, catalog adds, and the boot warm-sweep.
"""

import asyncio
import io
import logging
import os
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageOps

from config import ARTWORK_ROOT, LIBRARY_DIR
from database import SessionLocal
from models import ArtworkModel

logger = logging.getLogger("artwork-display-api")

# INFRA-D075 production incident (2026-09-25): opening the admin grid fired ~100 concurrent
# /artworks/{id}/thumbnail requests. Each handler queried the DB *then* awaited a slow Pillow
# render while still holding that FastAPI-injected session — SQLAlchemy keeps a session's
# connection checked out from the pool from first query until commit/rollback/close, so every one
# of those ~100 renders held a pool connection for the whole decode. With pool size 5 + overflow 10,
# the 16th concurrent request timed out (`QueuePool limit ... reached`), and the pool never
# recovered even after the renders finished (see `_image_decode_semaphore` below for the accompanying
# concurrency bound). Fix: any route that serves a derivative image must resolve what it needs from
# the DB in a SHORT session (closed immediately) and do all Pillow work with no session open.
def lookup_artwork_filename(artwork_id: int) -> str:
    """Look up an artwork's filename in a short-lived session that's closed before the caller does any
    slow image work. Raises HTTPException(404) if the artwork doesn't exist."""
    with SessionLocal() as db:
        art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
        if not art:
            raise HTTPException(404)
        return art.filename


# M6: process-wide cap on concurrent Pillow decode/encode work (thumbnail/preview/Canvas/e-ink
# derivatives) — a cold admin grid queues instead of thrashing a 2-vCPU box. Queued requests never
# hold a DB session (guaranteed by lookup_artwork_filename above + the callers' short-session pattern).
IMAGE_WORKERS = max(1, int(os.getenv("SD_IMAGE_WORKERS", "2")))
_image_decode_semaphore = asyncio.Semaphore(IMAGE_WORKERS)


async def run_image_work(fn, *args, cache_check=None, **kwargs):
    """Run blocking image work in the threadpool, bounded by `_image_decode_semaphore`. Callers must
    not hold a DB session across this call.

    M6 nit (reviewer, 2026-09-25): `cache_check`, if given, is tried FIRST — a cheap, synchronous
    probe (in-memory dict lookup, then at most one small disk read; see `peek_optimized_image` /
    `peek_canvas_image`). A hit returns immediately, WITHOUT ever taking the semaphore, so a warm
    derivative never queues behind someone else's cold render."""
    if cache_check is not None:
        hit = cache_check()
        if hit is not None:
            return hit
    async with _image_decode_semaphore:
        return await run_in_threadpool(fn, *args, **kwargs)


def _atomic_write(dst: Path, data: bytes) -> None:
    """Publish `data` to `dst` atomically (temp + os.replace) so a concurrent reader never sees a
    partial file. M6 nit: `tempfile.mkstemp` (not a PID-based name) — two THREADS in the same worker
    process share a pid, so a shared tmp name let concurrent renders of the same key interleave their
    writes before either replace() landed."""
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent, prefix=f"{dst.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, dst)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

# M4: caps for USER uploads only (routers/library.py /upload, routers/studio.py /upload/personal) —
# the untrusted-bytes path a LAN client controls directly, not server-side museum pack ingestion
# (core/lifespan.py, tools/build_pack.py), which keeps its existing, higher ceiling.
USER_UPLOAD_MAX_BYTES = 50 * 1024 * 1024   # 50 MB
USER_UPLOAD_MAX_PIXELS = 80_000_000        # 80 MP — below the 200 MP decompression-bomb ceiling in agents.py


async def read_capped_upload(file: UploadFile, request: Request, max_bytes: int | None = None) -> bytes:
    """Read an UploadFile's body with a hard cap, for untrusted user uploads. Content-Length is checked
    first as a fast pre-check, but never trusted alone (a client can omit/lie about it under chunked
    transfer) — the real enforcement is the streamed read, which stops as soon as the cap is crossed
    rather than buffering an arbitrarily large body into memory first.

    `max_bytes` is read from the module default at call time (not bound as a default arg) so tests can
    monkeypatch USER_UPLOAD_MAX_BYTES without exercising a real 50 MB body."""
    if max_bytes is None:
        max_bytes = USER_UPLOAD_MAX_BYTES
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        raise HTTPException(413, detail=f"File too large (max {max_bytes // (1024 * 1024)} MB).")
    buf = bytearray()
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, detail=f"File too large (max {max_bytes // (1024 * 1024)} MB).")
        buf.extend(chunk)
    return bytes(buf)


def check_user_upload_pixel_ceiling(width: int, height: int, max_pixels: int | None = None) -> None:
    """Reject a user-uploaded image above `max_pixels` — a lower ceiling than the global 200 MP
    decompression-bomb guard (agents.py), which still applies underneath this for every decode.
    Read from the module default at call time (see read_capped_upload) so it's monkeypatchable."""
    if max_pixels is None:
        max_pixels = USER_UPLOAD_MAX_PIXELS
    if width * height > max_pixels:
        raise HTTPException(400, detail=f"Image is too large (max {max_pixels // 1_000_000} MP).")


# M6 nit: the in-memory tier used to be a bare `@lru_cache`, which can't be peeked without calling
# (and thus potentially rendering) the function — needed a real "is this cached?" probe for
# `run_image_work`'s cache_check. A small hand-rolled LRU dict (move-to-end on hit, evict oldest on
# overflow) gives the same per-process/capped/gone-on-restart behavior as lru_cache, but peekable.
_OPT_CACHE_MAXSIZE = 256
_optimized_image_lru: "OrderedDict[tuple, bytes]" = OrderedDict()
_optimized_image_lru_lock = threading.Lock()


def _optimized_cache_get(key: tuple) -> Optional[bytes]:
    with _optimized_image_lru_lock:
        val = _optimized_image_lru.get(key)
        if val is not None:
            _optimized_image_lru.move_to_end(key)
        return val


def _optimized_cache_put(key: tuple, value: bytes) -> None:
    with _optimized_image_lru_lock:
        _optimized_image_lru[key] = value
        _optimized_image_lru.move_to_end(key)
        while len(_optimized_image_lru) > _OPT_CACHE_MAXSIZE:
            _optimized_image_lru.popitem(last=False)


def _optimized_image_mtime(image_path: Path) -> int:
    try:
        return int(image_path.stat().st_mtime)
    except OSError:
        return 0


def _optimized_derivative_path(image_path: Path, size: tuple, quality: int, mtime: int) -> Path:
    """M4 disk-cache path for a thumbnail/preview derivative — mirrors render_canvas_image's naming,
    keyed by (source filename, size, quality, source mtime) so an in-place file replace gets a fresh
    entry instead of serving stale bytes."""
    return DERIVATIVES_DIR / f"opt-{image_path.name}-{size[0]}x{size[1]}-q{quality}-{mtime}.jpg"


def peek_optimized_image(image_path: Path, size: tuple, quality: int = 85) -> Optional[bytes]:
    """Cheap, synchronous cache probe (in-memory, then at most one small disk read) — no Pillow, safe
    to call directly on the event loop. Returns None on a genuine miss, meaning the caller must render
    (see `run_image_work`'s `cache_check`, which uses this to skip the decode semaphore on a hit)."""
    mtime = _optimized_image_mtime(image_path)
    key = (image_path, size, quality, mtime)
    cached = _optimized_cache_get(key)
    if cached is not None:
        return cached
    dst = _optimized_derivative_path(image_path, size, quality, mtime)
    if dst.exists():
        try:
            data = dst.read_bytes()
        except OSError:
            return None
        _optimized_cache_put(key, data)
        return data
    return None


def get_optimized_image(image_path: Path, size: tuple, quality: int = 85) -> bytes:
    """Resize + JPEG-compress for web delivery. `mtime` participates only in the cache key (A4): a file
    replaced in place gets a fresh entry instead of serving stale bytes until process restart.

    M4: the in-memory dict is the first cache tier (per-process, capped, gone on restart); a disk-cache
    tier under ARTWORK_ROOT/_derivatives/ backs it so a restart doesn't re-decode every grid tile from
    the original — the boot warm-sweep (core/lifespan.py) and every subsequent restart's first hits
    both read this file instead of re-rendering (see `peek_optimized_image` for the read side)."""
    mtime = _optimized_image_mtime(image_path)
    key = (image_path, size, quality, mtime)
    cached = peek_optimized_image(image_path, size, quality)
    if cached is not None:
        return cached

    logger.info(f"[Image Processor] Optimizing: {image_path.name}")
    with Image.open(image_path) as img:
        if img.mode not in ("RGB", "L"):      # covers RGBA/P/LA/CMYK — "LA" used to crash the JPEG save
            img = img.convert("RGB")
        img.thumbnail(size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()

    dst = _optimized_derivative_path(image_path, size, quality, mtime)
    try:
        DERIVATIVES_DIR.mkdir(exist_ok=True)
        _atomic_write(dst, data)
        # M6 nit: prune sibling derivatives for this (source name, size, quality) at a DIFFERENT
        # mtime — an in-place edit/replace otherwise leaves an orphan on disk forever (unbounded
        # growth; a real concern on a Pi's SD card). Mirrors render_canvas_image's pruning.
        prefix = f"opt-{image_path.name}-{size[0]}x{size[1]}-q{quality}-"
        for old in DERIVATIVES_DIR.glob(f"{prefix}*.jpg"):
            if old != dst:
                try:
                    old.unlink()
                except OSError:
                    pass
    except OSError as e:
        logger.warning(f"[Image Processor] could not disk-cache {dst.name}: {e}")

    _optimized_cache_put(key, data)
    return data

# --- Canvas display image (resolution-capped) -------------------------------
# The Canvas <img> previously loaded the full-res original via /media. Museum
# originals can be 40–110 MB / 150+ MP — too big for a Pi-class browser to decode
# and GPU-texture (GL_MAX_TEXTURE_SIZE is commonly 8192), so the placard cycles
# while the image never paints. We serve a capped derivative instead; the full-res
# original stays on disk untouched (focal/crop quality unaffected). 7680 px long
# edge keeps ~4K detail even after a portrait→landscape cover-crop + Ken Burns
# zoom, while staying under the 8192 texture ceiling.
DISPLAY_MAX_EDGE = 7680
DISPLAY_QUALITY = 90
DERIVATIVES_DIR = ARTWORK_ROOT / "_derivatives"


def _canvas_derivative_path(art_id: int, mtime: int) -> Path:
    return DERIVATIVES_DIR / f"{art_id}-{mtime}-{DISPLAY_MAX_EDGE}.jpg"


def peek_canvas_image(src: Path, art_id: int) -> Optional[bytes]:
    """Cheap, synchronous cache probe for the Canvas display derivative — see `peek_optimized_image`
    for why `run_image_work` wants this (a hit must never queue behind someone else's cold render)."""
    try:
        mtime = int(src.stat().st_mtime)
    except OSError:
        return None
    dst = _canvas_derivative_path(art_id, mtime)
    if dst.exists():
        try:
            return dst.read_bytes()
        except OSError:
            return None
    return None


def render_canvas_image(src: Path, art_id: int) -> bytes:
    """Resolution-capped, EXIF-baked JPEG for the Canvas; disk-cached per source mtime.

    Heavy (decode + LANCZOS downscale + encode of a 150 MP original) — call via
    run_in_threadpool so it never blocks the event loop. The derivative is written
    once and then served from disk on every later request; the cap is only applied
    when the source actually exceeds it (smaller originals are re-encoded as-is)."""
    DERIVATIVES_DIR.mkdir(exist_ok=True)
    mtime = int(src.stat().st_mtime)
    dst = _canvas_derivative_path(art_id, mtime)
    if dst.exists():
        try:
            return dst.read_bytes()
        except OSError:
            pass  # fall through and re-render
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)   # bake orientation — a re-encode drops the EXIF tag
        if img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > DISPLAY_MAX_EDGE:
            img.thumbnail((DISPLAY_MAX_EDGE, DISPLAY_MAX_EDGE), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=DISPLAY_QUALITY, progressive=True)
        data = buf.getvalue()
    # Prune stale derivatives for this artwork (an earlier crop/replace → new mtime).
    for old in DERIVATIVES_DIR.glob(f"{art_id}-*.jpg"):
        if old != dst:
            try: old.unlink()
            except OSError: pass
    # Atomic publish (_atomic_write uses mkstemp — see its docstring for why a PID-based tmp name
    # wasn't unique enough once two threads in the same worker could render the same key at once).
    _atomic_write(dst, data)
    return data


def warm_canvas_cache_async(art_id: int, filename: str) -> None:
    """Fire-and-forget: pre-render the capped display derivative in the background so the Canvas never
    pays the one-time encode (up to several seconds for a 150 MP original) on first display. Best-effort;
    a missing loop or a bad file is swallowed (the lazy path + the boot sweep are the backstops)."""
    async def _run():
        try:
            await run_in_threadpool(render_canvas_image, LIBRARY_DIR / filename, art_id)
        except Exception as e:
            logger.warning(f"[Warm] could not pre-render display image for art {art_id}: {e}")
    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        pass  # no running loop (sync context) — the boot sweep will catch it
